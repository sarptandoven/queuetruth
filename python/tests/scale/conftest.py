"""Puts `scripts/scale` (the W1 pipeline scripts) and this directory on `sys.path` for the scale tests."""

from __future__ import annotations

import sys
from pathlib import Path

for _p in (Path(__file__).resolve().parents[3] / "scripts" / "scale", Path(__file__).resolve().parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
