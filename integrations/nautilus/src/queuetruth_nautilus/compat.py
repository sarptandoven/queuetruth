"""The NautilusTrader names this package needs, per major version (checked on 1.231.0 and 2.0.0rc7).

| what | 1.x (Cython) | 2.x (Rust, PyO3) |
|---|---|---|
| fill model | `nautilus_trader.backtest.models.FillModel` | `nautilus_trader.execution.DefaultFillModel` |
| latency model | `nautilus_trader.backtest.models.LatencyModel` | `nautilus_trader.execution.StaticLatencyModel` |
| book type | `nautilus_trader.model.enums.BookType` | `nautilus_trader.model.BookType` |
| venue config fill model | `ImportableFillModelConfig` (path + `FillModelConfig`) | the model object itself |

Both latency models take `cancel_latency_nanos` (not `delete_latency_nanos`); 1.x defaults `base_latency_nanos` to
1 ms and 2.x to 0, so this package always passes it. Importing this module does not import nautilus_trader.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from typing import Any

from queuetruth_nautilus.errors import QtNautilusError

#: Oldest release this package supports (the cross-checked 1.x release).
MIN_VERSION = (1, 231, 0)
#: Release lines verified against queuetruth's exact replay (`scripts/nt_crosscheck`).
VERIFIED_LINES = ((1, 231), (2, 0))

_VER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?")


@dataclass(frozen=True, order=True)
class NtVersion:
    """A parsed nautilus_trader version (`2.0.0rc7` -> 2, 0, 0, `rc`, 7)."""

    major: int
    minor: int
    patch: int
    pre: tuple[str, int] | None = None
    text: str = ""

    @property
    def verified(self) -> bool:
        """True for a release line queuetruth cross-checked against its exact replay."""
        return (self.major, self.minor) in VERIFIED_LINES


def parse_version(text: str) -> NtVersion:
    m = _VER.match(text.strip())
    if m is None:
        raise QtNautilusError("QT-USAGE-001", f"cannot parse nautilus_trader version {text!r}")
    pre = (m.group(4), int(m.group(5))) if m.group(4) else None
    return NtVersion(int(m.group(1)), int(m.group(2)), int(m.group(3)), pre, text.strip())


def installed_version() -> NtVersion:
    """The installed nautilus_trader version; `QT-USAGE-001` when it is missing or older than 1.231.0."""
    try:
        import nautilus_trader
    except ImportError as e:
        raise QtNautilusError(
            "QT-USAGE-001",
            "nautilus_trader is not installed",
            "pip install 'nautilus_trader>=1.231.0' (add --pre for the 2.0 release candidates)",
        ) from e
    v = parse_version(str(getattr(nautilus_trader, "__version__", "")))
    if (v.major, v.minor, v.patch) < MIN_VERSION:
        raise QtNautilusError(
            "QT-USAGE-001",
            f"nautilus_trader {v.text} is older than the oldest supported release 1.231.0",
            "upgrade nautilus_trader",
        )
    return v


def unverified_message(v: NtVersion) -> str | None:
    """The `CalibrationWarning` text for a version outside the cross-checked lines, None for a verified one."""
    if v.verified:
        return None
    return (
        f"nautilus_trader {v.text} has not been cross-checked against queuetruth's exact replay (verified: "
        "1.231.x, 2.0.x); run scripts/nt_crosscheck before relying on the calibrated fill rate"
    )


def resolve(path: str) -> Any:
    """`module:attribute` of nautilus_trader; `QT-USAGE-001` when the installed version lacks it."""
    mod, _, attr = path.partition(":")
    try:
        return getattr(importlib.import_module(mod), attr)
    except (ImportError, AttributeError) as e:
        raise QtNautilusError(
            "QT-USAGE-001", f"nautilus_trader does not provide {path} ({e})", "check the installed nautilus_trader"
        ) from e


def fill_model_path(v: NtVersion | None = None) -> tuple[str, str]:
    """(module, attribute) of the probabilistic fill model of version `v` (default: installed)."""
    v = v or installed_version()
    return (
        ("nautilus_trader.execution", "DefaultFillModel")
        if v.major >= 2
        else ("nautilus_trader.backtest.models", "FillModel")
    )


def latency_model_path(v: NtVersion | None = None) -> tuple[str, str]:
    """(module, attribute) of the static latency model of version `v` (default: installed)."""
    v = v or installed_version()
    if v.major >= 2:
        return ("nautilus_trader.execution", "StaticLatencyModel")
    return ("nautilus_trader.backtest.models", "LatencyModel")


def fill_model_cls(v: NtVersion | None = None) -> Any:
    return resolve(":".join(fill_model_path(v)))


def latency_model_cls(v: NtVersion | None = None) -> Any:
    return resolve(":".join(latency_model_path(v)))


def book_type_l2(v: NtVersion | None = None) -> Any:
    """`BookType.L2_MBP` of the installed version."""
    v = v or installed_version()
    bt = resolve("nautilus_trader.model:BookType" if v.major >= 2 else "nautilus_trader.model.enums:BookType")
    return bt.L2_MBP


def describe(obj: Any) -> str:
    """Readable text of a fill or latency model of either version (1.x objects have no informative repr)."""
    if obj is None:
        return "None"
    name = type(obj).__name__
    names = (
        "prob_fill_on_limit",
        "prob_slippage",
        "base_latency_nanos",
        "insert_latency_nanos",
        "update_latency_nanos",
        "cancel_latency_nanos",
    )
    fields = [f for f in names if hasattr(obj, f)]
    if fields:
        return f"{name}({', '.join(f'{f}={getattr(obj, f)!r}' for f in fields)})"
    return repr(obj)
