"""`scripts/scale/local_day.py` sizes its unit pool to the memory the machine has (no units are run here)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import local_day as D


def test_workers_fit_in_memory() -> None:
    # a 16 GB runner (about 15 GiB available) holds two 6 GiB units, not three: the 2026-10-09 IEX day was killed
    # out of memory with three
    assert D.cap_workers(3, 6, 15.0) == 2
    assert D.cap_workers(4, 6, 64.0) == 4
    # never below one, so a small machine still makes progress
    assert D.cap_workers(3, 6, 4.0) == 1
    # unknown memory, or the cap turned off, leaves the asked number
    assert D.cap_workers(3, 6, None) == 3 and D.cap_workers(3, 0, 15.0) == 3


def test_available_memory_is_read_here() -> None:
    mem = D.avail_mem_gib()
    assert mem is None or mem > 0


def test_sample_budget_follows_the_workers_that_fit() -> None:
    # a budget sized for four units at a time shrinks in proportion when memory holds three, and is kept otherwise
    assert D.scaled_budget(10.0, 4, 3) == 7.5
    assert D.scaled_budget(10.0, 4, 4) == 10.0 and D.scaled_budget(None, 4, 1) is None
    # a budget of 0 is a budget (one symbol per tier), not "no budget"
    assert D.scaled_budget(0.0, 4, 2) == 0.0


def test_no_combine_refuses_a_site(capsys) -> None:
    # the site is built from the single-day combine, so the two cannot go together; refused before any work
    with pytest.raises(SystemExit):
        D.main(["--source", "iex", "--date", "2026-10-09", "--no-combine", "--site"])
    assert "--no-combine" in capsys.readouterr().err


def test_day_problem_names_each_unusable_day(tmp_path: Path) -> None:
    import json

    import atlas
    import pyarrow as pa
    import pyarrow.parquet as pq

    def day(name: str, units_valid: int, cells: pa.Table) -> Path:
        d = tmp_path / name
        d.mkdir()
        (d / "day_manifest.json").write_text(json.dumps({"units_valid": units_valid, "units_planned": 2}))
        pq.write_table(cells, d / "cells_all.parquet")
        return d

    rows = {"symbol": ["A", "B"], "n": [3, 4]}
    assert atlas.day_problem(day("ok", 2, pa.table({**rows, "exact_ok": [True, False]}))) is None
    assert atlas.day_problem(day("none", 0, pa.table({}))).startswith("no valid unit (0 of 2")
    assert atlas.day_problem(day("empty", 1, pa.table({}))).startswith("no cells")
    flagged = atlas.day_problem(day("flagged", 2, pa.table({**rows, "exact_ok": [False, False]})))
    assert flagged.startswith("every symbol-day is flagged")
