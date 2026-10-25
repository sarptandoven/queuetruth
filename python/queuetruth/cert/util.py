"""Small shared helpers: the issuing clock, atomic JSON writes and the environment record."""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError

#: Distributions whose versions are recorded (when installed): the ones whose numerics shape outputs.
PACKAGES = ("numpy", "pyarrow", "scipy", "scikit-learn", "pydantic", "cryptography")
#: Environment variables recorded when set (thread pools of numeric libraries).
ENV_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "PYTHONHASHSEED")


def now_ns() -> int:
    """Issue time in ns: `SOURCE_DATE_EPOCH` (seconds, the reproducible-builds convention) when set, else now."""
    sde = os.environ.get("SOURCE_DATE_EPOCH")
    if sde is not None:
        try:
            v = int(sde)
        except ValueError:
            raise QtError("QT-CONFIG-001", f"SOURCE_DATE_EPOCH={sde!r}: expected integer seconds") from None
        if v < 0:
            raise QtError("QT-CONFIG-001", f"SOURCE_DATE_EPOCH={sde!r}: must not be negative")
        return v * 1_000_000_000
    return time.time_ns()


def write_text(text: str, path: Path, *, private: bool = False) -> None:
    """Writes `text` atomically (temporary file in the same directory, then rename); `private` = mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600 if private else 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json(doc: dict[str, Any], path: Path, *, private: bool = False) -> None:
    """Writes a JSON document (2-space indent, UTF-8, no NaN) atomically."""
    write_text(json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n", path, private=private)


def environment() -> dict[str, Any]:
    """The `Environment` fields of the running interpreter (no hostname, user name or path)."""
    from queuetruth._version import __version__
    from queuetruth.schemas.arrow import ENGINE_VERSION

    try:
        from queuetruth import _core

        core: str | None = str(_core.version())
    except ImportError:
        core = None
    pkgs: dict[str, str] = {}
    for name in PACKAGES:
        try:
            pkgs[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return {
        "queuetruth": __version__,
        "core": core,
        "engine_version": ENGINE_VERSION,
        "python": platform.python_version(),
        "implementation": sys.implementation.name,
        "platform": f"{platform.system()}-{platform.release()}",
        "machine": platform.machine(),
        "packages": pkgs,
        "env": {k: os.environ[k] for k in ENV_VARS if k in os.environ},
    }
