"""Puts `scripts/scale` (the atlas pipeline, whose Merkle and canonical JSON rules are pinned here) on `sys.path`."""

from __future__ import annotations

import sys
from pathlib import Path

_SCALE = Path(__file__).resolve().parents[3] / "scripts" / "scale"
if str(_SCALE) not in sys.path:
    sys.path.insert(0, str(_SCALE))
