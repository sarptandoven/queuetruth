"""`scripts/scale/local_day.py` sizes its unit pool to the memory the machine has (no units are run here)."""

from __future__ import annotations

import sys
from pathlib import Path

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
