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

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from queuetruth.atlas._lookup import AtlasResult
    from queuetruth.fit import FitResult


def lookup(
    library: str | None = None,
    symbol: str | None = None,
    tier: str | None = None,
    horizon: str | int | None = None,
    source: str | Path | None = None,
    *,
    yes: bool = False,
    refresh: bool = False,
    confirm: Callable[[str], bool] | None = None,
    fetch: Callable[[str], bytes] | None = None,
) -> AtlasResult:
    """The atlas answer for `library` (`hftbacktest`, `nautilus`, `databento`; all three by default), one `symbol` or
    a liquidity `tier`, at `horizon` (any `queuetruth.units` duration: `"1s"`, `"1min"`; default the site's): the
    answer in plain words, every setting's bias with its 95% interval, and the closest setting's config to paste
    (`queuetruth.atlas._lookup.lookup`). `source` is a directory or URL of the data API; nothing is downloaded unless
    `yes=True` or `confirm(url)` returns True. Returns an `AtlasResult` (`queuetruth.results.Result`)."""
    from queuetruth.atlas._lookup import lookup as _lookup

    return _lookup(
        library,
        symbol,
        tier,
        None if horizon is None else str(horizon),
        source,
        yes=yes,
        refresh=refresh,
        confirm=confirm,
        fetch=fetch,
    )


def calibration(
    library: str,
    tier: str | None = None,
    symbol: str | None = None,
    source: str | Path | None = None,
    *,
    yes: bool = False,
    refresh: bool = False,
    confirm: Callable[[str], bool] | None = None,
    fetch: Callable[[str], bytes] | None = None,
) -> FitResult:
    """The atlas's closest setting of `library` (`nautilus`, `hftbacktest`, `databento`) in a liquidity `tier`
    (`T1_top100` or `T1`; default `ALL`) or the tier of `symbol`, as the `FitResult` `qt.fit` returns
    (`queuetruth.atlas._calibration.calibration`). Nothing is downloaded unless `yes=True` or `confirm(url)` returns
    True."""
    from queuetruth.atlas._calibration import calibration as _calibration

    return _calibration(
        library, tier=tier, symbol=symbol, source=source, yes=yes, refresh=refresh, confirm=confirm, fetch=fetch
    )


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


def open_source(source: Any = None, **kwargs: Any) -> Any:
    """The atlas data API at a directory or URL (default: the public atlas, through the cache; nothing is downloaded
    without consent), as `queuetruth.atlas._lookup.open_source`: `.manifest`, `.has(path)` and `.get(path)` (each
    file checked against the manifest's sha256 and its model)."""
    from queuetruth.atlas._lookup import open_source as _open

    return _open(source, **kwargs)


def tier_name(tier: str) -> str:
    """A liquidity tier in plain words (`T1_top100` -> `the 100 most traded symbols`)."""
    from queuetruth.atlas._common import TIER_WORDS

    return TIER_WORDS.get(tier, tier)


def confirm_on_terminal(interactive: bool, what: str = "atlas", local: str = "--from DIR") -> Any:
    """The consent callback of `queuetruth atlas`: asks on a terminal before a download, else stops with
    `QT-USAGE-001` and the `--yes` hint. `what` names the command and `local` its local-copy flag in the messages."""
    from queuetruth.atlas._cli import _confirm

    return _confirm(interactive, what, local)


__all__ = ["calibration", "confirm_on_terminal", "lookup", "open_source", "tier_name", "touch_tables"]
