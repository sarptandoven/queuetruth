"""Unit tests of the report helpers: formatting, Wilson intervals, reliability bins, strata, downsampling, ticks and
the SVG chart builders (well-formed, deterministic)."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import numpy as np
import pytest

from queuetruth.report import fmt as F
from queuetruth.report.compute import last_per_bucket, reliability_bins, strata, wilson
from queuetruth.report.svg import (
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
    time_ticks,
)


def test_format_helpers() -> None:
    assert F.dur(100_000_000) == "100 ms"
    assert F.dur(1_000_000_000) == "1 s"
    assert F.dur(300_000_000_000) == "5 min"
    assert F.dur(15_000_000_000) == "15 s"
    assert F.dur(250_000) == "250 µs"
    assert F.dur(1_000_000_000_000) == "1000 s"
    assert F.dur(None) == F.DASH
    assert F.ts(1_577_716_200_123_456_789) == "2019-12-30 14:30:00.123456789"
    assert F.price(100_250_000_000, -9) == "100.25"
    assert F.price(100_250_000_000, None) == "100250000000"
    assert F.pp(0.031) == "+3.1 pp" and F.pp(-0.0) == "+0.0 pp"
    assert F.pval(3e-6) == "3.0e-06" and F.pval(0.04) == "0.040"
    assert F.num(1234567) == "1,234,567" and F.num(0.012345) == "0.01235" and F.num(float("nan")) == F.DASH
    assert F.esc('<a href="x">&') == "&lt;a href=&quot;x&quot;&gt;&amp;"
    assert F.date_of(18260) == "2019-12-30"


def test_wilson_interval() -> None:
    lo, hi = wilson(3, 10)
    assert lo == pytest.approx(0.10779126740630104, abs=1e-12)
    assert hi == pytest.approx(0.6032218525388546, abs=1e-12)
    assert wilson(0, 10)[0] == 0.0 and wilson(10, 10)[1] == 1.0
    assert wilson(0, 0) == (0.0, 1.0)


def test_reliability_bins() -> None:
    p = np.array([0.0, 0.05, 0.15, 0.95, 1.0, 1.0])
    y = np.array([0, 0, 1, 1, 1, 0])
    bins = reliability_bins(p, y)
    assert len(bins) == 10
    assert bins[0].n == 2 and bins[0].obs == 0.0
    assert bins[1].n == 1 and bins[1].obs == 1.0
    assert bins[9].n == 3 and bins[9].obs == pytest.approx(2 / 3)
    assert sum(b.n for b in bins) == 6
    for b in bins:
        if b.n:
            assert b.ci_lo <= b.obs <= b.ci_hi


def test_strata_deciles_per_group() -> None:
    v = np.arange(20, dtype=float)
    g = np.array(["a"] * 10 + ["b"] * 10, dtype=object)
    s = strata(v, g, 10)
    assert s[:10].tolist() == list(range(10)) and s[10:].tolist() == list(range(10))
    v2 = v.copy()
    v2[3] = np.nan
    assert strata(v2, g, 10)[3] == -1


def test_last_per_bucket() -> None:
    t = np.arange(10_000, dtype=np.int64)
    v = np.arange(10_000)
    tt, vv = last_per_bucket(t, v, 0, 9_999, 100)
    assert len(tt) == 100 and tt[-1] == 9_999 and vv[-1] == 9_999
    assert np.all(np.diff(tt) > 0)
    t2, _ = last_per_bucket(t[:50], v[:50], 0, 49, 100)
    assert len(t2) == 50


def test_ticks() -> None:
    assert nice_ticks(0.0, 1.0, 5) == [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    assert nice_ticks(-0.034, 0.071, 5)[0] <= -0.034 and nice_ticks(-0.034, 0.071, 5)[-1] >= 0.071
    lo, hi, t = log_ticks(5e7, 3e11)
    assert lo == 1e7 and hi == 1e12 and t[0] == 1e7
    ticks, f = time_ticks(1_577_716_200_000_000_000 + 7 * 10**9, 3 * 3600)
    assert all(f(x)[-2:] in ("00", "30") for x in ticks) and len(ticks) >= 4


def _xml(s: str) -> ET.Element:
    return ET.fromstring(s)


def test_chart_builders_are_wellformed_and_deterministic() -> None:
    series = [
        Series(
            "live", [(1e8, 0.1), (1e9, None), (1e10, 0.3)], slot=1, markers=True, ci=[(1e8, 0.0, 0.2), (1e10, 0.2, 0.4)]
        ),
        Series("sim <&>", [(1e8, 0.2), (1e9, 0.25), (1e10, 0.4)], slot=2, kind="step", dashed=True),
    ]
    a = line_chart("u1", "T <x>", "D", series, x_log=True, hrefs=[Ref(0.0, "zero")], vrefs=[Ref(1e9, "v")])
    b = line_chart("u1", "T <x>", "D", series, x_log=True, hrefs=[Ref(0.0, "zero")], vrefs=[Ref(1e9, "v")])
    assert a == b
    root = _xml(a)
    assert root.attrib["aria-labelledby"] == "u1-t u1-d"
    assert "sim &lt;&amp;&gt;" in a
    _xml(bar_chart("u2", "B", "D", ["a", "b"], [("x", 1, [1.0, None]), ("y", 2, [-2.0, 3.0])]))
    _xml(
        forest_chart(
            "u3", "F", "D", [("m1", 0.1, -0.1, 0.3, "tip"), ("m2", None, None, None, "none")], band=(-0.02, 0.02)
        )
    )
    _xml(heatmap("u4", "H", "D", ["r1", "r2"], ["c1", "c2"], [[0.1, None], [-0.2, 0.0]]))
    bins = [RelBin(0.0, 0.1, 5, 0.05, 0.2, 0.03, 0.6), RelBin(0.1, 0.2, 0, 0.15, 0.0, 0.0, 0.0)]
    _xml(reliability_panel("u5", "R", "D", bins, label="1 s"))
    empty = line_chart("u6", "E", "D", [Series("only", [])])
    _xml(empty)
