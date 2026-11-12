"""`fixtures synth`: the synthetic quickstart dataset."""

from __future__ import annotations

from queuetruth.ops._call import _call, _parse_doc
from queuetruth.schemas import models as m


def fixtures_synth(out: str, venue: str = "cme", sessions: int = 2) -> m.FixturesSummary:
    """`fixtures synth`: the synthetic quickstart dataset (synth_small generator) with truth tables."""
    text = _call("fixtures_synth", str(out), venue, int(sessions))
    return _parse_doc(m.FixturesSummary, text, "queuetruth._core.fixtures_synth")
