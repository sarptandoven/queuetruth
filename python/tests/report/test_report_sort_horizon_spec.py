"""Schema 1.1 `ReportSpec.leaderboard_sort_horizon_ns` (CLI `report --sort-horizon`): `render` sorts the zoo
leaderboard at the spec's horizon; the `leaderboard_sort_horizon_ns` keyword overrides it; null keeps 60 s."""

from __future__ import annotations

from pathlib import Path

import pytest

from queuetruth.errors import QtError
from queuetruth.report import render
from report import pageparse as pp
from report import synth as rc

S = 1_000_000_000


def _zoo_section(html: str) -> str:
    """The leaderboard section (answer-first layout: the leaderboard is its own section, not part of the zoo)."""
    return pp.section(html, "leaderboard")


def _first_row(html: str) -> str:
    """The first model row of the compact leaderboard (after the exact fill-rate row, 0.1.1)."""
    rows = pp.leaderboard_rows(html)
    assert rows and rows[0] == "exact fill rate (truth)"
    return rows[1]


def test_spec_sort_horizon_is_used_and_the_keyword_wins(tmp_path: Path) -> None:
    rc.make_zoo_dir(tmp_path / "zoo", n=80)
    doc = rc.json_load(tmp_path / "zoo" / "summary.json")
    board = doc["leaderboard"]

    def best(h: int) -> str:
        return min(board, key=lambda e: (abs(e["fill_rate_bias"][str(h)]["value"]), e["brier"][str(h)], e["model_id"]))[
            "model_id"
        ]

    def html(name: str, **spec: object) -> str:
        kw = {k: spec.pop(k) for k in list(spec) if k == "keyword"}
        out = tmp_path / name / "report.html"
        render(
            {"inputs": [str(tmp_path / "zoo")], "out": str(out), **spec},
            **({"leaderboard_sort_horizon_ns": kw["keyword"]} if kw else {}),
        )
        return out.read_text(encoding="utf-8")

    page = html("spec", leaderboard_sort_horizon_ns=10 * S)
    s = _zoo_section(page)
    assert "sorted by absolute fill-rate bias at 10 s" in s and _first_row(page) == rc.esc(best(10 * S))
    s = _zoo_section(html("default"))
    assert "sorted by absolute fill-rate bias at 60 s" in s
    s = _zoo_section(html("kw", leaderboard_sort_horizon_ns=60 * S, keyword=10 * S))
    assert "sorted by absolute fill-rate bias at 10 s" in s
    with pytest.raises(QtError) as e:
        html("bad", leaderboard_sort_horizon_ns=0)
    assert e.value.code == "QT-CONFIG-001"
