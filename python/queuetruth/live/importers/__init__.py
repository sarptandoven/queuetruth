"""Venue importers (SPEC 5.7.1), simulator-output importers and trading-bot importers; every importer maps
`(input paths, ImportOptions) -> ImportResult` (see `queuetruth.live.builder` for the shared semantics).

Each importer module declares an `ImporterDef` next to its code; `registry()` collects them, with third-party ones
from the `queuetruth.importers` entry-point group (`queuetruth.live.importers._registry` documents both).
`VENUE_IMPORTERS`, `SIM_IMPORTERS` and `BOT_IMPORTERS` are read-only views of the registry kept for compatibility:
name -> importer function."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from queuetruth.live.builder import ImportResult
from queuetruth.live.importers._base import (
    KINDS,
    Importer,
    ImporterDef,
    ImporterKind,
    ImporterOptions,
    ImportOptions,
)
from queuetruth.live.importers._registry import (
    ENTRY_POINT_GROUP,
    LIVE_IMPORTERS_RS,
    LIVE_VENUES_PY,
    ImporterWarning,
    builtins,
    by_venue,
    generated,
    get,
    importers,
    live_importers_rs,
    live_venues,
    live_venues_py,
    names,
    option_rows,
    register,
    registry,
    reload,
    unregister,
)


class _KindView(Mapping[str, Importer]):
    """Read-only view: importer name -> importer function, for one kind (built-ins and plugins, in list order)."""

    def __init__(self, kind: str) -> None:
        self._kind = kind

    def _map(self) -> dict[str, Importer]:
        return {d.name: d.run for d in importers(self._kind)}

    def __getitem__(self, name: str) -> Importer:
        return self._map()[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._map())

    def __len__(self) -> int:
        return len(self._map())

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._kind!r}: {list(self._map())})"


#: The venue importers of SPEC 5.7.1 (`LiveImportSpec.venue` values): name -> importer.
VENUE_IMPORTERS: Mapping[str, Importer] = _KindView("venue")
#: Simulator-output importers (`LiveImportSpec.venue` `sim_<name>`; see `queuetruth.live.import_sim`).
SIM_IMPORTERS: Mapping[str, Importer] = _KindView("sim")
#: Trading-bot importers of the live arm (Freqtrade trades database + bot log, Hummingbot database / trades CSV);
#: `import_freqtrade_live` / `import_hummingbot_live` write their logs.
BOT_IMPORTERS: Mapping[str, Importer] = _KindView("bot")


def __getattr__(name: str) -> Any:
    """The importer functions by their own name (`import_binance`), as this package exported them before."""
    if name.startswith("import_"):
        for d in builtins():
            if getattr(d.run, "__name__", None) == name:
                return d.run
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BOT_IMPORTERS",
    "ENTRY_POINT_GROUP",
    "KINDS",
    "LIVE_IMPORTERS_RS",
    "LIVE_VENUES_PY",
    "SIM_IMPORTERS",
    "VENUE_IMPORTERS",
    "ImportOptions",
    "ImportResult",
    "Importer",
    "ImporterDef",
    "ImporterKind",
    "ImporterOptions",
    "ImporterWarning",
    "builtins",
    "by_venue",
    "generated",
    "get",
    "importers",
    "live_importers_rs",
    "live_venues",
    "live_venues_py",
    "names",
    "option_rows",
    "register",
    "registry",
    "reload",
    "unregister",
]
