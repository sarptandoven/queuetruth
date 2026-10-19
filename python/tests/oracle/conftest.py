"""Puts the oracle package (`qt_oracle`, next to this file) on sys.path for the oracle tests."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
