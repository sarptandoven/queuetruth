"""queuetruth: fill-realism and queue-truth kit (SPEC 4.3). Re-exports the Python API.

`queuetruth.score`, `queuetruth.report`, `queuetruth.export`, `queuetruth.check`, `queuetruth.fit` and
`queuetruth.compare` are both subpackages and API functions (`qt.score(spec)`, `qt.report(spec)`, `qt.export(spec)`,
`qt.check(backtest, data=...)`, `qt.fit(backtester, data=...)`, `qt.compare(backtest, live)`): the subpackage
modules are callable and delegate to the `queuetruth.api` function of the same name, so `import queuetruth.score as
s` and `qt.score(spec)` both work.
`qt.HazardModel` is imported on first use (it needs scikit-learn), and so are `qt.backtesters` (the backtester plugin
registry), `qt.backtrader` and `qt.lean` (fill models and logs for those frameworks, which stay optional),
`qt.atlas` (`qt.atlas.lookup`) and `qt.demo` (a subpackage and the function `qt.demo(backtester=...)`).

Every verb returns a result of one shape (`queuetruth.results.Result`): `.summary`, `.table()`, `.to_html()`, a
notebook view, `.doc` and `.save(path)`.
"""

import importlib
import inspect
import sys
import types
from typing import Any

from queuetruth import schemas
from queuetruth._version import __version__
from queuetruth.api import (
    Session,
    calibrate,
    calibrate_live,
    extract_features,
    fit_hazard,
    fixtures_synth,
    import_live,
    ingest,
    mirror_real_orders,
    read_table,
    referee,
    referee_fixtures,
    replay,
    run_zoo,
    sample_shadows,
    shadows_from_live,
    validate_live,
    why,
)
from queuetruth.errors import QtApiError, QtError

#: Attributes imported on first access (PEP 562): `HazardModel` pulls in scikit-learn (about 1 s), which no CLI
#: command but `fit-hazard` needs.
_LAZY = {"HazardModel": "queuetruth.model"}
#: Submodules imported on first access, so `qt.backtrader.QueueFiller` works after `import queuetruth as qt`
#: (the framework modules import their framework only when used), and `qt.data` (`queuetruth data add`,
#: `data fetch iex`).
_LAZY_MODULES = ("atlas", "backtesters", "backtrader", "data", "demo", "lean")


def __getattr__(name: str) -> Any:
    if name in _LAZY_MODULES:
        return importlib.import_module(f"queuetruth.{name}")
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'queuetruth' has no attribute {name!r}")
    value = getattr(importlib.import_module(target), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY, *_LAZY_MODULES})


_CALLABLE_NAMES = ("score", "report", "export", "check", "fit", "compare", "demo")
#: The function a callable subpackage runs, where it is not the `queuetruth.api` function of the same name (`demo`
#: is a product, above `queuetruth.api`), and the function whose typed signature it shows (`qt.check`, `qt.fit`,
#: `qt.compare`: the `queuetruth.api` functions forward `**kwargs`).
_TARGETS = {
    "check": "queuetruth.check.run:check",
    "fit": "queuetruth.fit.run:fit",
    "compare": "queuetruth.compare.run:compare",
    "demo": "queuetruth.demo.result:demo",
}


def _target(name: str) -> Any:
    where = _TARGETS.get(name)
    if where is None:
        return getattr(importlib.import_module("queuetruth.api"), name)
    mod, _, attr = where.partition(":")
    return getattr(importlib.import_module(mod), attr)


class _CallableModule(types.ModuleType):
    """A subpackage that is also the `queuetruth.api` function of the same name (`demo`: `queuetruth.demo.result`).
    `inspect.signature(qt.check)` is the function's typed signature."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return _target(self.__name__.rsplit(".", 1)[-1])(*args, **kwargs)

    @property
    def __signature__(self) -> inspect.Signature:
        return inspect.signature(_target(self.__name__.rsplit(".", 1)[-1]))


class _Package(types.ModuleType):
    """The `queuetruth` package: whatever the import system binds to `score` / `report` / `export` (also after a
    submodule is removed from `sys.modules` and imported again) is made callable, so `qt.score(spec)` never turns
    into a plain module."""

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _CALLABLE_NAMES and isinstance(value, types.ModuleType):
            value.__class__ = _CallableModule
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _Package

score = importlib.import_module("queuetruth.score")
report = importlib.import_module("queuetruth.report")
export = importlib.import_module("queuetruth.export")
check = importlib.import_module("queuetruth.check")
fit = importlib.import_module("queuetruth.fit")
compare = importlib.import_module("queuetruth.compare")
for _m in (score, report, export, check, fit, compare):
    _m.__class__ = _CallableModule
del _m

__all__ = [
    "HazardModel",
    "QtApiError",
    "QtError",
    "Session",
    "__version__",
    "atlas",
    "calibrate",
    "calibrate_live",
    "check",
    "compare",
    "data",
    "demo",
    "export",
    "extract_features",
    "fit",
    "fit_hazard",
    "fixtures_synth",
    "import_live",
    "ingest",
    "mirror_real_orders",
    "read_table",
    "referee",
    "referee_fixtures",
    "replay",
    "report",
    "run_zoo",
    "sample_shadows",
    "schemas",
    "score",
    "shadows_from_live",
    "validate_live",
    "why",
]
