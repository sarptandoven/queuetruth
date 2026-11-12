"""The Fill-Realism Atlas as data: the versioned data API the site publishes under `atlas/v1/`, and its reader.

    import queuetruth as qt
    answer = qt.atlas.lookup(library="nautilus", symbol="AAPL")   # asks before it downloads anything
    print(answer.summary)

Layout of this package: every public module is one file of the data API and defines its document model and a
`DOC_MODELS` dict (`manifest.py` for `manifest.json`, `summary.py`, `configs.py`, `symbols.py`, and `answer.py`
for what `queuetruth atlas --format json` prints). `_registry.doc_models()` finds them, so a new API file is one new
module and nothing else. Private modules hold the code: `_common` (labels, units, wording), `_lookup` (reading,
fetching and the answer) and `_cli` (`queuetruth atlas`).
"""

from __future__ import annotations

from typing import Any


def lookup(**kwargs: Any) -> Any:
    """The atlas answer for a library, symbol, tier and horizon (`queuetruth.atlas._lookup.lookup`)."""
    from queuetruth.atlas._lookup import lookup as _lookup

    return _lookup(**kwargs)


__all__ = ["lookup"]
