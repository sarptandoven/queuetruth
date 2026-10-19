"""Degenerate inputs never crash the report (a ZeroDivisionError in the y scale was once found): property tests of
every chart builder and tick helper over empty, single-point, constant, non-finite, huge and tiny data, plus
end-to-end renders of degenerate zoo / scorecard directories, and leg matching through the schedule's `order_key`
(live legs are named `<order_key>.leg<k>`)."""

from __future__ import annotations

import itertools
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from queuetruth.errors import QtError
from queuetruth.report import render, why
from queuetruth.report.compute import alloc_profile
from queuetruth.report.svg import (
    Frame,
    Ref,
    RelBin,
    Series,
    bar_chart,
    forest_chart,
    heatmap,
    line_chart,
    log_ticks,
    nice_ticks,
    reliability_panel,
    safe_range,
    time_ticks,
)
from report import synth as rc

SPECIAL = [0.0, -0.0, 1.0, -1.0, 1e-300, -1e-300, 1e300, -1e300, 1.5e18, 1e-17, math.nan, math.inf, -math.inf]
floats = st.one_of(st.sampled_from(SPECIAL), st.floats(allow_nan=True, allow_infinity=True))
opt_floats = st.one_of(st.none(), floats)
PROP = settings(max_examples=150, deadline=None, database=None, suppress_health_check=[HealthCheck.too_slow])


def _ok_svg(s: str) -> None:
    root = ET.fromstring(s)
    assert root.tag.endswith("svg")
    for bad in ("nan", "inf"):
        for el in root.iter():
            for k, v in el.attrib.items():
                if k in ("class", "id", "aria-labelledby", "role", "text-anchor"):
                    continue
                assert bad not in v.lower(), (el.tag, k, v)


@PROP
@given(floats, floats, st.integers(min_value=0, max_value=12))
def test_nice_ticks_always_two_increasing_finite(lo: float, hi: float, n: int) -> None:
    t = nice_ticks(lo, hi, n)
    assert len(t) >= 2 and all(math.isfinite(x) for x in t)
    assert all(b > a for a, b in itertools.pairwise(t))
    if math.isfinite(lo) and math.isfinite(hi) and abs(lo) < 1e250 and abs(hi) < 1e250:
        assert t[0] <= min(lo, hi) + 1e-9 * max(1.0, abs(lo)) and t[-1] >= max(lo, hi) - 1e-9 * max(1.0, abs(hi))


@PROP
@given(floats, floats)
def test_log_ticks_and_safe_range(lo: float, hi: float) -> None:
    a, b, t = log_ticks(lo, hi)
    assert 0 < a < b and math.isfinite(a) and math.isfinite(b) and len(t) >= 2 and len(t) <= 40
    r0, r1 = safe_range(lo, hi)
    assert math.isfinite(r0) and math.isfinite(r1) and r1 > r0


@PROP
@given(floats, floats, floats)
def test_frame_scales_never_divide_by_zero(v: float, lo: float, hi: float) -> None:
    fr = Frame(100, 100, 10, 10, 10, 10, x_lo=lo, x_hi=hi, y_lo=lo, y_hi=hi)
    for log in (False, True):
        fr.x_log = log
        assert math.isfinite(fr.sx(v)) and math.isfinite(fr.sy(v))


def test_time_ticks_degenerate() -> None:
    for span in (0.0, -5.0, math.nan, 1e-9):
        ticks, f = time_ticks(1_577_716_200_000_000_000, span)
        assert len(ticks) >= 2 and f(ticks[0])


points = st.lists(st.tuples(floats, opt_floats), max_size=6)


@PROP
@given(
    st.lists(points, max_size=3),
    st.booleans(),
    st.sampled_from(["line", "step", "points"]),
    st.one_of(st.none(), st.tuples(floats, floats)),
    st.one_of(st.none(), st.tuples(floats, floats)),
    st.booleans(),
    st.lists(floats, max_size=3),
)
def test_line_chart_degenerate(
    series: list[Any], x_log: bool, kind: str, xr: Any, yr: Any, diag: bool, ticks: list[float]
) -> None:
    ser = [
        Series(
            f"s{i}",
            pts,
            slot=i % 4 + 1,
            kind=kind,
            markers=True,
            ci=[(x, y, y) for x, y in pts],
            ci_style="band" if i % 2 else "whisker",
            tips=["t"] * (len(pts) - 1),
        )
        for i, pts in enumerate(series)
    ]
    _ok_svg(
        line_chart(
            "u",
            "T",
            "D",
            ser,
            x_log=x_log,
            x_range=xr,
            y_range=yr,
            diag=diag,
            x_ticks=ticks or None,
            hrefs=[Ref(math.nan), Ref(0.0, "z")],
            vrefs=[Ref(math.inf), Ref(1.0, "v")],
            xbands=[(math.nan, 1.0, "b"), (0.0, 1.0, "b")],
        )
    )


@PROP
@given(st.lists(st.lists(opt_floats, min_size=0, max_size=4), max_size=3), st.integers(min_value=0, max_value=4))
def test_bar_chart_degenerate(groups: list[list[float | None]], ncat: int) -> None:
    cats = [f"c{i}" for i in range(ncat)]
    _ok_svg(bar_chart("u", "B", "D", cats, [(f"g{i}", 1, g) for i, g in enumerate(groups)]))


@PROP
@given(st.lists(st.tuples(opt_floats, opt_floats, opt_floats, opt_floats), max_size=5), st.tuples(floats, floats))
def test_forest_and_heatmap_degenerate(rows: list[Any], band: Any) -> None:
    fr = [(f"m{i}", e, lo, hi, "t") for i, (e, lo, hi, _) in enumerate(rows)]
    _ok_svg(forest_chart("u", "F", "D", fr, band=band, alt=[r[3] for r in rows]))
    vals = [[r[0], r[1], r[2]] for r in rows]
    _ok_svg(heatmap("u", "H", "D", [f"r{i}" for i in range(len(rows))], ["a", "b", "c"], vals))
    _ok_svg(heatmap("u", "H", "D", ["r"], ["a"], [[0.0]], vmax=0.0))


@PROP
@given(st.lists(st.tuples(st.integers(min_value=0, max_value=5), floats, floats, floats, floats), max_size=10))
def test_reliability_panel_degenerate(bins: list[Any]) -> None:
    rb = [RelBin(0.0, 0.1, n, p, o, lo, hi) for n, p, o, lo, hi in bins]
    _ok_svg(reliability_panel("u", "R", "D", rb, label="x"))


def test_alloc_profile_degenerate_grids() -> None:
    z = np.array([])
    assert alloc_profile("power3", z, z, z, z, (-3.0, 4.0, 5))[0][1] == 0.0
    one = np.array([1.0])
    for fam in ("power", "power2", "power3", "databento"):
        for grid in ((0.0, 0.0, 1), (-1.0, 1.0, 3), (-3.0, 4.0, 2)):
            prof = alloc_profile(fam, one, one, one, one, grid)
            assert len(prof) == grid[2]


# ---- end to end -------------------------------------------------------------------------------------------------


def _render(tmp: Path, inputs: list[Any], **kw: Any) -> str:
    out = tmp / "r.html"
    render({"inputs": [str(i) for i in inputs], "out": str(out), **kw})
    html = out.read_text(encoding="utf-8")
    for svg in re.findall(r"<svg\b.*?</svg>", html, re.S):
        _ok_svg(svg)
    return html


def test_degenerate_zoo_directories(tmp_path: Path) -> None:
    for n, errs, traj in ((1, True, True), (2, False, False), (5, True, False)):
        d = tmp_path / f"zoo{n}"
        rc.make_zoo_dir(d, n=n, with_errors=errs, with_traj=traj)
        _render(d, [d])
    # every forecast and every truth identical, one model, one horizon
    d = tmp_path / "flat"
    d.mkdir()
    n = 4
    cols = {f.name: [] for f in rc.SCHEMAS["qt.zoo_predictions"]}
    for i in range(n):
        for k, v in dict(
            model_id="front_of_queue[mode=faithful]",
            shadow_id=f"s{i}",
            qty=1,
            horizon_ns=10**9,
            p_fill=0.5,
            e_fill_qty=0.5,
            model_ts_first_fill=None,
            truth_filled=False,
            truth_ts_first_fill=None,
            truth_filled_qty=0,
            censored=False,
            queue_ahead_at_live=7,
            level_qty_at_live=7,
            visible_level_qty_at_live=7,
            out_of_view_at_live=False,
            instrument_id=1,
            session_date=rc.DAY,
            ts_live=rc.OPEN,
            trade_vol_60s=0,
        ).items():
            cols[k].append(v)
    rc.write(d, "zoo_predictions.parquet", "qt.zoo_predictions", cols)
    rc.write_doc(
        d / "summary.json",
        rc.ZooSummary(
            run_id="00000000000000aa",
            spec={"h_max_ns": 10**9},
            n_shadows=n,
            models=["front_of_queue[mode=faithful]"],
            leaderboard=[
                {
                    "model_id": "front_of_queue[mode=faithful]",
                    "brier": {"1000000000": 0.25},
                    "logloss": {"1000000000": None},
                    "fill_rate_bias": {"1000000000": {"value": 0.5, "ci_lo": 0.5, "ci_hi": 0.5}},
                    "overfill_bias": {"value": None, "ci_lo": None, "ci_hi": None},
                    "qty_bias": None,
                    "ttf_iad": None,
                    "mcnemar": {"b": 0, "c": 0, "p_exact": None},
                }
            ],
            files=[],
            warnings=[],
        ),
    )
    rc.write(
        d,
        "zoo_errors.parquet",
        "qt.zoo_errors",
        {
            "model_id": ["front_of_queue[mode=faithful]"] * 2,
            "metric": ["fill_rate_bias", "fill_rate_bias_aj"],
            "horizon_ns": [10**9] * 2,
            "stratum_kind": ["queue_decile", "all"],
            "stratum": ["0", "all"],
            "value": [0.0, 0.0],
            "ci_lo": [0.0, None],
            "ci_hi": [0.0, None],
            "n": [n, n],
        },
    )
    # open conflict 2 (OC-7): zoo predictions without their planned-life table are refused, not read by the flag
    with pytest.raises(QtError) as ei:
        _render(d, [d])
    assert ei.value.code == "QT-INPUT-001" and "zoo_shadows" in ei.value.message
    ts = rc.pa.timestamp("ns", tz="UTC")
    zs = rc.pa.table(
        {
            "shadow_id": rc.pa.array([f"s{i}" for i in range(n)], rc.pa.utf8()),
            "ts_cancel_planned": rc.pa.array([None] * n, ts),
            "ts_admin_end": rc.pa.array([None] * n, ts),
            "instrument_id": rc.pa.array([1] * n, rc.pa.uint32()),
            "session_date": rc.pa.array([rc.DAY] * n, rc.pa.date32()),
            "ts_live": rc.pa.array([rc.OPEN] * n, ts),
            "session_close": rc.pa.array([rc.OPEN + 3600 * 10**9] * n, ts),
            "exact_end_state": rc.pa.array(["open_at_end"] * n, rc.pa.utf8()),
            "never_cancel": rc.pa.array([True] * n, rc.pa.bool_()),
        }
    )
    rc.write_table(zs.cast(rc.SCHEMAS["qt.zoo_shadows"]), d / "zoo_shadows.parquet", rc.SCHEMAS["qt.zoo_shadows"])
    html = _render(d, [d])
    assert '<section id="zoo">' in html


def test_degenerate_position_and_scorecard(tmp_path: Path) -> None:
    d = tmp_path / "pos"
    d.mkdir()
    rc.write(
        d,
        "position.parquet",
        "qt.score_position",
        {"arm": ["live"], "instrument_id": [1], "ts": [rc.OPEN], "position": [0]},
    )
    assert '<section id="position">' in _render(d, [d])
    s = tmp_path / "score"
    rc.make_score_dir(s, n=3, with_why=False)
    _render(s, [s])


def test_scorecard_legs_matched_by_order_key(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(why, "WHY_PROVIDER", None)
    f = rc.make_score_dir(tmp_path / "score", n=200, with_why=False)
    key = f["sim_only_key"]
    legs = [f"{key}.leg1", f"{key}.leg2"]
    sched = {fld.name: [] for fld in rc.SCHEMAS["qt.shadow_spec"]}
    for i, sid in enumerate([*legs, "other.leg1"]):
        for k, v in dict(
            shadow_id=sid,
            instrument_id=1,
            side=ord("B"),
            price_mode="abs",
            price=1,
            offset_ticks=0,
            qty=1,
            ts_submit=rc.OPEN + i,
            latency_ns=0,
            ts_cancel=None,
            group=None,
            source="live_log",
            order_key=key if sid in legs else "other",
        ).items():
            sched[k].append(v)
    rc.write(tmp_path / "score", "schedule.parquet", "qt.shadow_spec", sched)
    tr = {fld.name: [] for fld in rc.SCHEMAS["qt.queue_trajectory"]}
    for j, sid in enumerate([*legs, "other.leg1"]):
        for k, v in dict(
            shadow_id=sid,
            ts_event=rc.OPEN + 10**9 * (j + 1),
            sequence=j,
            kind="event",
            queue_ahead_before=50,
            queue_ahead_after=40 - j,
            rank_before=5,
            rank_after=4,
            level_qty_before=90,
            level_qty_after=80,
            cancel_ahead_qty=10,
            cancel_behind_qty=0,
            add_behind_qty=0,
            traded_at_px_qty=0,
            trade_through_qty=0,
            filled_qty=0,
        ).items():
            tr[k].append(v)
    rc.write(tmp_path / "score", "queue_trajectory.parquet", "qt.queue_trajectory", tr)
    html = _render(tmp_path, [tmp_path / "score"], max_why=1)
    sec = re.search(r'<section id="why">(.*?)</section>', html, re.S).group(1)
    assert "Exact queue trajectory (2 events" in sec
    assert rc.esc(legs[0]) in sec and rc.esc(legs[1]) in sec and "other.leg1" not in sec
