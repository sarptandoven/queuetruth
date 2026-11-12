"""The importer registry: every venue, simulator and trading-bot importer, read by every consumer.

Each importer module declares its importers next to its code (`IMPORTER = ImporterDef(...)`, or a tuple
`IMPORTERS`); the registry finds them by scanning the modules of `queuetruth.live.importers`, so adding a venue is one
new file. A third-party package adds one with no change to queuetruth, through the `queuetruth.importers`
entry-point group:

    # pyproject.toml of the plugin
    [project.entry-points."queuetruth.importers"]
    myvenue = "myvenue_qt:IMPORTER"

The entry point names an `ImporterDef`, a tuple or list of them, or a function that returns either. A plugin cannot
replace a built-in. The consumers: `live import --venue` (choices and help), `LiveImportSpec.venue` (through the
generated `queuetruth.schemas._live_venues`), its JSON Schema enum and the Rust `LiveVenue`, the service's `qt.meta`,
`queuetruth.live.import_venue` / `import_sim` / `import_bot`, the compatibility views `VENUE_IMPORTERS`,
`SIM_IMPORTERS`, `BOT_IMPORTERS`, and docs/reference/importers.md.

    from queuetruth.live import importers
    importers.get("binance").formats          # what it reads
    importers.names("sim")                    # the simulator importers
    importers.by_venue("ibkr-flex").options   # the typed options model
"""

from __future__ import annotations

import dataclasses
import importlib
import pkgutil
import warnings
from collections.abc import Iterable
from typing import Any

from queuetruth.errors import QtError
from queuetruth.live.importers._base import KINDS, ImporterDef, ImporterOptions, type_text

#: The entry-point group third-party importers register under.
ENTRY_POINT_GROUP = "queuetruth.importers"
_PACKAGE = "queuetruth.live.importers"


class ImporterWarning(UserWarning):
    """A plugin of the `queuetruth.importers` entry-point group could not be loaded or clashes with another."""


_BUILTINS: tuple[ImporterDef, ...] | None = None
_REGISTRY: dict[str, ImporterDef] | None = None
_EXTRA: dict[str, ImporterDef] = {}


def _order(d: ImporterDef) -> tuple[int, int, str]:
    return (d.rank, KINDS.index(d.kind), d.name)


def _defs_of(obj: Any, where: str) -> list[ImporterDef]:
    """The `ImporterDef`s an entry point or module attribute names (one, several, or a function returning them)."""
    if callable(obj) and not isinstance(obj, ImporterDef):
        obj = obj()
    items = [obj] if isinstance(obj, ImporterDef) else list(obj) if isinstance(obj, Iterable) else [obj]
    return [_valid(d, where) for d in items]


def _valid(d: Any, where: str) -> ImporterDef:
    if not isinstance(d, ImporterDef):
        raise TypeError(f"{where}: not an ImporterDef ({type(d).__name__})")
    name = d.name
    if not isinstance(name, str) or not name or not all(c.isalnum() or c == "_" for c in name) or name != name.lower():
        raise TypeError(f"{where}: importer name must be lowercase letters, digits or _, got {name!r}")
    if d.kind not in KINDS:
        raise TypeError(f"{where}: importer kind must be one of {list(KINDS)}, got {d.kind!r}")
    if d.kind != "sim" and name.startswith("sim_"):
        raise TypeError(f"{where}: only simulator importers take the sim_ venue prefix, got {name!r}")
    if not callable(d.run):
        raise TypeError(f"{where}: run is not callable")
    if not (isinstance(d.options, type) and issubclass(d.options, ImporterOptions)):
        raise TypeError(f"{where}: options must be an ImporterOptions subclass")
    return d


def builtins() -> tuple[ImporterDef, ...]:
    """The importers shipped with queuetruth, in list order: every `IMPORTER` / `IMPORTERS` of the modules of
    `queuetruth.live.importers` (modules whose name starts with `_` are skipped)."""
    global _BUILTINS
    if _BUILTINS is None:
        pkg = importlib.import_module(_PACKAGE)
        found: dict[str, ImporterDef] = {}
        for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
            if info.name.startswith("_"):
                continue
            mod = importlib.import_module(f"{_PACKAGE}.{info.name}")
            for attr in ("IMPORTER", "IMPORTERS"):
                if hasattr(mod, attr):
                    for d in _defs_of(getattr(mod, attr), f"{mod.__name__}.{attr}"):
                        if d.venue in found:
                            raise TypeError(f"{mod.__name__}: importer venue {d.venue!r} is declared twice")
                        found[d.venue] = d
        _BUILTINS = tuple(sorted(found.values(), key=_order))
    return _BUILTINS


def _load() -> dict[str, ImporterDef]:
    from importlib.metadata import entry_points

    reg: dict[str, ImporterDef] = {d.venue: d for d in builtins()}
    plugins: list[ImporterDef] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        where = f"entry point {ep.name} = {ep.value}"
        try:
            defs = _defs_of(ep.load(), where)
        except Exception as e:  # a broken plugin must not break queuetruth
            warnings.warn(f"{where}: not loaded ({type(e).__name__}: {e})", ImporterWarning, stacklevel=3)
            continue
        dist = getattr(getattr(ep, "dist", None), "name", None) or "unknown"
        for d in defs:
            if d.venue in reg or any(p.venue == d.venue for p in plugins):
                warnings.warn(f"{where}: importer {d.venue!r} is already registered; ignored", ImporterWarning, 3)
                continue
            plugins.append(dataclasses.replace(d, origin=f"{dist}:{ep.name}"))
    for d in [*plugins, *_EXTRA.values()]:
        reg.setdefault(d.venue, d)
    return dict(sorted(reg.items(), key=lambda kv: (not kv[1].builtin, _order(kv[1]))))


def registry() -> dict[str, ImporterDef]:
    """Every registered importer by `LiveImportSpec.venue` value: the built-ins first, then plugins (loaded once;
    `reload` reads the entry points again)."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _load()
    return _REGISTRY


def reload() -> None:
    """Forgets the loaded plugins; the next lookup reads the entry points again."""
    global _REGISTRY
    _REGISTRY = None


def register(d: ImporterDef) -> ImporterDef:
    """Registers an importer for this process (for tests and notebooks; packages use the entry point). Refuses a
    venue that is already registered."""
    d = _valid(d, "register")
    if d.venue in registry():
        raise QtError("QT-CONFIG-001", f"importer {d.venue!r} is already registered")
    d = dataclasses.replace(d, origin="register")
    _EXTRA[d.venue] = d
    registry()[d.venue] = d
    return d


def unregister(venue: str) -> None:
    """Removes an importer added with `register` (built-ins and entry points stay)."""
    if _EXTRA.pop(venue, None) is not None and _REGISTRY is not None:
        _REGISTRY.pop(venue, None)


def importers(kind: str | None = None, *, builtin: bool = False) -> tuple[ImporterDef, ...]:
    """The registered importers in list order (`kind`: only `venue`, `sim` or `bot`; `builtin`: no plugins)."""
    defs = builtins() if builtin else tuple(registry().values())
    return tuple(d for d in defs if kind is None or d.kind == kind)


def names(kind: str | None = None, *, builtin: bool = False) -> tuple[str, ...]:
    """Importer names (`sim` importers without the `sim_` prefix), as `importers`."""
    return tuple(d.name for d in importers(kind, builtin=builtin))


def live_venues(*, builtin: bool = False) -> tuple[str, ...]:
    """Every `LiveImportSpec.venue` value, in list order."""
    return tuple(d.venue for d in importers(builtin=builtin))


def by_venue(venue: str) -> ImporterDef:
    """The importer of a `LiveImportSpec.venue` value (the CLI spelling `ibkr-flex` works too); `QT-CONFIG-001`
    names the registered ones when none matches."""
    d = registry().get(venue) or registry().get(venue.replace("-", "_"))
    if d is None:
        raise QtError("QT-CONFIG-001", f"no importer for venue {venue!r}; registered: {', '.join(registry())}")
    return d


def get(name: str, kind: str = "venue") -> ImporterDef:
    """The `kind` importer called `name` (`get("hftbacktest", "sim")`)."""
    for d in importers(kind):
        if d.name == name:
            return d
    raise QtError("QT-CONFIG-001", f"no {kind} importer {name!r}; registered: {', '.join(names(kind))}")


# ---- generated files -----------------------------------------------------------------------------------------------

#: The contracts copy of the built-in venue list (`LiveImportSpec.venue`), written by `make generate`.
LIVE_VENUES_PY = "python/queuetruth/schemas/_live_venues.py"


def live_venues_py() -> str:
    """`python/queuetruth/schemas/_live_venues.py`: the built-in `LiveImportSpec.venue` values, so the contracts layer
    does not import the importers."""
    venues = live_venues(builtin=True)
    lines = [
        '"""The `LiveImportSpec.venue` values: the built-in importers of `queuetruth.live.importers.registry()`.',
        "",
        "@generated by `make generate` (scripts/gen_importers.py). Do not edit: `make generate-check` fails when it",
        "is stale.",
        '"""',
        "",
        "LIVE_IMPORT_VENUES: tuple[str, ...] = (",
        *[f'    "{v}",' for v in venues],
        ")",
    ]
    return "\n".join(lines) + "\n"


def option_rows(d: ImporterDef) -> list[tuple[str, str, str]]:
    """(name, type, description) of each of an importer's own options."""
    rows = []
    for name in sorted(d.options.option_names()):
        f = d.options.model_fields[name]
        rows.append((name, type_text(f.annotation), (f.description or "").strip()))
    return rows


#: The `live import` rules of the Rust CLI grammar, written by `make generate`.
LIVE_IMPORTERS_RS = "crates/cli/src/live_importers.rs"


def live_importers_rs() -> str:
    """`crates/cli/src/live_importers.rs`: which built-in venues take `--fix-*` flags and `--option KEY=VALUE`."""
    defs = importers(builtin=True)
    fix = [d.venue for d in defs if "fix" in d.options.option_names()]
    opt = [d.venue for d in defs if d.options.option_names() - {"fix", "tz"}]

    def arr(head: str, xs: list[str]) -> list[str]:
        # as rustfmt lays it out: one line when it fits and every element is short, else one element per line
        quoted = [f'"{x}"' for x in xs]
        one = f"{head}{', '.join(quoted)}];"
        if len(one) <= 120 and all(len(x) + 2 <= 10 for x in xs):
            return [one]
        return [head, *[f'    "{x}",' for x in xs], "];"]

    lines = [
        "//! @generated by `make generate` (scripts/gen_importers.py) from the importer registry",
        "//! (`queuetruth.live.importers`). Do not edit: `make generate-check` fails when it is stale.",
        "",
        "/// `live import` venues (spec spelling) whose importer reads the `--fix-*` flags.",
        *arr("pub(crate) const FIX_VENUES: &[&str] = &[", fix),
        "",
        "/// `live import` venues (spec spelling) whose importer has options of its own (`--option KEY=VALUE`).",
        *arr("pub(crate) const OPTION_VENUES: &[&str] = &[", opt),
    ]
    return "\n".join(lines) + "\n"


def generated() -> dict[str, str]:
    """Every file generated from the registry, keyed by path relative to the repository root."""
    return {LIVE_VENUES_PY: live_venues_py(), LIVE_IMPORTERS_RS: live_importers_rs()}
