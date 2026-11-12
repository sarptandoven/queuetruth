"""The `Backtester` plugin interface and its registry.

A backtester is one entry that says how queuetruth reads and talks to it: its name, a label, the importer of its
output (simulator log), and optional parts: an importer of a live run, a replay adapter for `export
replay-schedule`, a snippet writer for calibrations, the zoo models that replicate its fill models and a reconciler.
`check` also uses its `detect` to tell its files from other backtesters' files.

The built-ins (hftbacktest, nautilus, freqtrade, hummingbot, backtrader, vectorbt, lean) are thin entries over the
existing importers. A third-party package adds one with no change to queuetruth, through the `queuetruth.backtesters`
entry-point group:

    # pyproject.toml of the plugin
    [project.entry-points."queuetruth.backtesters"]
    mysim = "mysim_qt:BACKTESTER"

The entry point names a `Backtester` (an object with the attributes below; `BacktesterEntry` is the ready-made
one), or a class or function that returns one. `check --backtester mysim`, `queuetruth.live.import_sim("mysim",
...)` and `reconcile --kind mysim` (when it has a reconciler) then find it. A plugin cannot replace a built-in.

    from queuetruth import backtesters
    backtesters.get("lean").label       # 'QuantConnect LEAN'
    backtesters.names(check=True)       # the backtesters `check` reads
"""

from __future__ import annotations

import importlib
import json
import warnings
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from queuetruth.errors import QtError

#: The entry-point group third-party backtesters register under.
ENTRY_POINT_GROUP = "queuetruth.backtesters"
_HEAD = 256 * 1024


@dataclass(frozen=True)
class Probe:
    """What `detect` reads of a file once, for every backtester: the first bytes, the column or key names and, for
    JSON, the parsed document. `binary` is true for a zip archive or an SQLite database (no columns are read)."""

    path: Path
    magic: bytes
    columns: frozenset[str]
    doc: Any = None
    binary: bool = False

    @classmethod
    def of(cls, path: str | Path) -> Probe:
        """Reads `path` (`QT-INPUT-001` when it is missing or unreadable)."""
        p = Path(path)
        if not p.is_file():
            raise QtError("QT-INPUT-001", f"{p}: no such file")
        with p.open("rb") as f:
            magic = f.read(16)
        if magic.startswith((b"PK\x03\x04", b"SQLite format 3\x00")):
            return cls(p, magic, frozenset(), None, True)
        cols, doc = _columns(p)
        return cls(p, magic, frozenset(cols), doc, False)


def _keys_of_json(text: str) -> tuple[set[str], Any]:
    """Keys of the first JSON object of `text` (a JSON document or JSON lines) and the parsed document if any."""
    s = text.lstrip()
    doc: Any = None
    try:
        doc = json.loads(s)
    except ValueError:
        line = s.split("\n", 1)[0]
        try:
            doc = json.loads(line)
        except ValueError:
            return set(), None
    first = doc[0] if isinstance(doc, list) and doc else doc
    return (set(first) if isinstance(first, dict) else set()), doc


def _columns(path: Path) -> tuple[set[str], Any]:
    """(column or key names, parsed JSON document or None) of a backtest file."""
    suffix = path.suffix.lower()
    if suffix in (".parquet", ".pq"):
        import pyarrow.parquet as pq

        try:
            return set(pq.read_schema(path).names), None
        except Exception as e:  # unreadable Parquet
            raise QtError("QT-INPUT-001", f"{path}: unreadable Parquet ({e})") from e
    try:
        with path.open("rb") as f:
            raw = f.read(_HEAD)
    except OSError as e:
        raise QtError("QT-INPUT-001", f"{path}: {e}") from e
    text = raw.decode("utf-8", errors="replace")
    if text.lstrip()[:1] in ("{", "["):
        if len(raw) == _HEAD:  # too large to parse a prefix as one document: read it whole
            text = path.read_text(encoding="utf-8", errors="replace")
        return _keys_of_json(text)
    header = text.split("\n", 1)[0]
    return {c.strip().strip('"') for c in header.split(",")}, None


@runtime_checkable
class Backtester(Protocol):
    """What queuetruth needs to know about a backtester. Only `name`, `label` and `import_sim` are required; every
    other attribute may be None (or empty) when the backtester does not have that part.

    - `name`: the registry key (`check --backtester NAME`, `import_sim(NAME, ...)`); lowercase letters, digits, `_`.
    - `label`: the name people know it by, for messages.
    - `import_sim(paths, options) -> ImportResult`: its output -> canonical live-log rows
      (`queuetruth.live.importers.Importer`; `options` is an `ImportOptions`).
    - `import_live(inputs, out, *, account, instrument_map, append, **options) -> LiveImportSummary`: a live or
      dry-run run of a trading bot, written as a canonical live log.
    - `replay(out, **kwargs) -> ReplayExport`: writes a program that replays a live order schedule in it (`export
      replay-schedule`).
    - `snippet(calibration) -> (python_text, notes)`: the code to paste for a calibration result.
    - `zoo_models`: the queuetruth zoo families that replicate its fill models (e.g. `nt_prob_touch`).
    - `reconcile(sim, live, out, **kwargs) -> BotReconcileReport`: its run against a live run, order by order.
    - `detect(probe) -> bool`: whether a file (a `Probe`) is its output; None when `check` cannot tell its files.
    - `detect_priority`: the order `detect` is tried in (lower first; built-ins use 10 to 60, plugins default 100).
    """

    name: str
    label: str
    import_sim: Callable[..., Any]
    import_live: Callable[..., Any] | None
    replay: Callable[..., Any] | None
    snippet: Callable[..., Any] | None
    zoo_models: Sequence[str]
    reconcile: Callable[..., Any] | None
    detect: Callable[[Probe], bool] | None
    detect_priority: int


@dataclass(frozen=True)
class BacktesterEntry:
    """A ready-made `Backtester`: fill in what the backtester has."""

    name: str
    label: str
    import_sim: Callable[..., Any]
    import_live: Callable[..., Any] | None = None
    replay: Callable[..., Any] | None = None
    snippet: Callable[..., Any] | None = None
    zoo_models: tuple[str, ...] = ()
    reconcile: Callable[..., Any] | None = None
    detect: Callable[[Probe], bool] | None = None
    detect_priority: int = 100
    #: where it came from: `builtin`, or the distribution and entry point of a plugin
    origin: str = field(default="builtin", compare=False)


def lazy(target: str) -> Callable[..., Any]:
    """A function that imports `module:attribute` on its first call and forwards to it (keeps the registry light and
    free of import cycles)."""
    mod, _, attr = target.partition(":")
    cache: list[Callable[..., Any]] = []

    def call(*args: Any, **kwargs: Any) -> Any:
        if not cache:
            cache.append(getattr(importlib.import_module(mod), attr))
        return cache[0](*args, **kwargs)

    call.__name__ = attr
    call.__qualname__ = attr
    call.__doc__ = f"Calls `{mod}.{attr}` (imported on first use)."
    return call


class BacktesterWarning(UserWarning):
    """A plugin of the `queuetruth.backtesters` entry-point group could not be loaded or clashes with another."""


_REGISTRY: dict[str, Backtester] | None = None
_EXTRA: dict[str, Backtester] = {}


def _valid(b: Any, where: str) -> Backtester:
    if isinstance(b, type) or (callable(b) and not hasattr(b, "import_sim")):
        b = b()
    missing = [a for a in ("name", "label", "import_sim") if getattr(b, a, None) is None]
    if missing:
        raise TypeError(f"{where}: not a Backtester (missing {', '.join(missing)})")
    name = b.name
    if not isinstance(name, str) or not name or not all(c.isalnum() or c == "_" for c in name) or name != name.lower():
        raise TypeError(f"{where}: backtester name must be lowercase letters, digits or _, got {name!r}")
    return b


def _load() -> dict[str, Backtester]:
    from importlib.metadata import entry_points

    from queuetruth.backtesters._builtin import BUILTINS

    reg: dict[str, Backtester] = {b.name: b for b in BUILTINS}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        where = f"entry point {ep.name} = {ep.value}"
        try:
            b = _valid(ep.load(), where)
        except Exception as e:  # a broken plugin must not break queuetruth
            warnings.warn(f"{where}: not loaded ({type(e).__name__}: {e})", BacktesterWarning, stacklevel=3)
            continue
        if b.name in reg:
            warnings.warn(f"{where}: backtester {b.name!r} is already registered; ignored", BacktesterWarning, 3)
            continue
        reg[b.name] = b
    for name, b in _EXTRA.items():
        reg.setdefault(name, b)
    return reg


def registry() -> dict[str, Backtester]:
    """Every registered backtester by name: the built-ins first, then plugins (loaded once; `reload` reads the
    entry points again)."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _load()
    return _REGISTRY


def reload() -> None:
    """Forgets the loaded plugins; the next lookup reads the entry points again."""
    global _REGISTRY
    _REGISTRY = None


def register(backtester: Any) -> Backtester:
    """Registers a backtester for this process (for tests and notebooks; packages use the entry point). Refuses a
    name that is already registered."""
    b = _valid(backtester, "register")
    if b.name in registry():
        raise QtError("QT-CONFIG-001", f"backtester {b.name!r} is already registered")
    _EXTRA[b.name] = b
    registry()[b.name] = b
    return b


def unregister(name: str) -> None:
    """Removes a backtester added with `register` (built-ins and entry points stay)."""
    if _EXTRA.pop(name, None) is not None and _REGISTRY is not None:
        _REGISTRY.pop(name, None)


def get(name: str) -> Backtester:
    """The backtester called `name` (`QT-CONFIG-001` when none is)."""
    b = registry().get(name)
    if b is None:
        raise QtError("QT-CONFIG-001", f"no backtester {name!r}; registered: {', '.join(registry())}")
    return b


def names(*, check: bool = False, reconcile: bool = False) -> tuple[str, ...]:
    """Registered names, built-ins first; `check`: only those `check` can detect; `reconcile`: only those with a
    reconciler."""
    out = list(builtin_names(check=check, reconcile=reconcile))
    for n, b in registry().items():
        if n in out or n in _builtin_set():
            continue
        if check and getattr(b, "detect", None) is None:
            continue
        if reconcile and getattr(b, "reconcile", None) is None:
            continue
        out.append(n)
    return tuple(out)


def _builtin_set() -> frozenset[str]:
    from queuetruth.backtesters._builtin import BUILTINS

    return frozenset(b.name for b in BUILTINS)


def builtin_names(*, check: bool = False, reconcile: bool = False) -> tuple[str, ...]:
    """As `names`, the built-ins only (CLI choices and help text do not change with the plugins installed)."""
    from queuetruth.backtesters._builtin import BUILTINS

    out = [b for b in BUILTINS if not (check and b.detect is None) and not (reconcile and b.reconcile is None)]
    if reconcile:  # the trading bots (reconciled against their own live run) first
        out.sort(key=lambda b: b.import_live is None)
    return tuple(b.name for b in out)


class Names(tuple[str, ...]):
    """CLI choices: lists the built-ins (help text and generated grammar stay fixed) and also accepts any registered
    backtester with the part asked for."""

    _check: bool
    _reconcile: bool

    def __new__(cls, *, check: bool = False, reconcile: bool = False) -> Names:
        self = super().__new__(cls, builtin_names(check=check, reconcile=reconcile))
        self._check, self._reconcile = check, reconcile
        return self

    def __contains__(self, item: object) -> bool:
        return tuple.__contains__(self, item) or item in names(check=self._check, reconcile=self._reconcile)


def detect(path: str | Path) -> str | None:
    """The backtester that wrote `path`, or None when the file does not say (read from its columns or keys, never
    from its name alone)."""
    probe = Probe.of(path)
    order = sorted(
        (b for b in registry().values() if getattr(b, "detect", None) is not None),
        key=lambda b: getattr(b, "detect_priority", 100),
    )
    for b in order:
        if b.detect is not None and b.detect(probe):
            return b.name
    return None


def zoo_models() -> Iterator[tuple[str, str]]:
    """(backtester, zoo family) pairs of every registered backtester."""
    for n, b in registry().items():
        for z in getattr(b, "zoo_models", ()) or ():
            yield n, z


def _zip_names(path: Path) -> list[str] | None:
    try:
        return zipfile.ZipFile(path).namelist()
    except zipfile.BadZipFile:
        return None


__all__ = [
    "ENTRY_POINT_GROUP",
    "Backtester",
    "BacktesterEntry",
    "BacktesterWarning",
    "Names",
    "Probe",
    "builtin_names",
    "detect",
    "get",
    "lazy",
    "names",
    "register",
    "registry",
    "reload",
    "unregister",
    "zoo_models",
]
