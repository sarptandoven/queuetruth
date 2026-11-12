"""The Fill-Realism Atlas as data: the versioned data API the site publishes under `atlas/v1/`, and its reader.

    import queuetruth as qt
    answer = qt.atlas.lookup(library="nautilus", symbol="AAPL")   # asks before it downloads anything
    print(answer.summary)
    cal = qt.atlas.calibration("nautilus", tier="T1_top100")       # the closest setting, as qt.fit returns it

Layout of this package: every public module is one file of the data API and defines its document model and a
`DOC_MODELS` dict (`manifest.py` for `manifest.json`, `summary.py`, `configs.py`, `symbols.py`, and `answer.py`
for what `queuetruth atlas --format json` prints). `_registry.doc_models()` finds them, so a new API file is one new
module and nothing else. Private modules hold the code: `_common` (labels, units, wording), `_lookup` (reading,
fetching and the answer), `_calibration` (`calibration`) and `_cli` (`queuetruth atlas`).
"""

from __future__ import annotations

from typing import Any


def lookup(**kwargs: Any) -> Any:
    """The atlas answer for a library, symbol, tier and horizon (`queuetruth.atlas._lookup.lookup`)."""
    from queuetruth.atlas._lookup import lookup as _lookup

    return _lookup(**kwargs)


def calibration(library: str, tier: str | None = None, symbol: str | None = None, **kwargs: Any) -> Any:
    """The atlas's closest setting of `library` as the `FitResult` `qt.fit` returns
    (`queuetruth.atlas._calibration.calibration`)."""
    from queuetruth.atlas._calibration import calibration as _calibration

    return _calibration(library, tier=tier, symbol=symbol, **kwargs)


def touch_tables(source: Any = None, *, yes: bool = False, **kwargs: Any) -> Any:
    """The atlas touch tables (`atlas/v1/touch.json`) as a `queuetruth.atlas.touch.TouchTables`, read through the same
    consent-based fetch and cache as `lookup` (nothing is downloaded unless `yes=True` or `confirm(url)` says yes).
    `QT-INPUT-001` when the atlas has no touch tables yet."""
    from queuetruth.atlas._lookup import open_source
    from queuetruth.atlas.touch import TouchTables
    from queuetruth.errors import QtError

    src = open_source(source, yes=yes, **kwargs)
    if not src.has("touch.json"):
        m = src.manifest
        raise QtError(
            "QT-INPUT-001",
            f"atlas: {src.where} has no touch tables yet (atlas/v1/touch.json; built {m['built_at']})",
            "pass table= a touch.json file, or wait until the atlas publishes them (docs/concepts/touch-tables.md)",
        )
    return TouchTables.model_validate(src.get("touch.json"))


__all__ = ["calibration", "lookup", "touch_tables"]
