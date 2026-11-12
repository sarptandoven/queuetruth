"""Parts shared by the atlas data API documents (Pydantic and `queuetruth.schemas.models` base classes only)."""

from __future__ import annotations

from typing import Literal

from queuetruth.schemas.models import QtModel

#: The libraries of the data API (`queuetruth.atlas._common.LIBRARY_NAMES`): `bound` holds the two bounds that
#: bracket every honest model, `other` any family the atlas does not name.
Library = Literal["hftbacktest", "nautilus", "databento", "bound", "other"]
#: Whether a setting is still clearly off: only a 95% interval that excludes 0 on the side of its estimate says so.
Verdict = Literal["too_optimistic", "too_pessimistic", "no_clear_difference"]


class Interval(QtModel):
    """A point estimate with its 95% interval (from the atlas's bootstrap); `lo` and `hi` are null when the atlas
    has no interval (a symbol seen on one day)."""

    value: float | None
    lo: float | None
    hi: float | None


__all__ = ["Interval", "Library", "Verdict"]
