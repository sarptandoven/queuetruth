"""The user cache directory of queuetruth (stdlib only): `QT_CACHE_DIR` when set, else the platform's convention.

- macOS: `~/Library/Caches/queuetruth`
- Windows: `%LOCALAPPDATA%\\queuetruth\\Cache`
- other (Linux, BSD): `$XDG_CACHE_HOME/queuetruth`, default `~/.cache/queuetruth`
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def user_cache_dir(env: dict[str, str] | None = None, platform: str | None = None) -> Path:
    """The cache root (module docstring); `env` and `platform` default to the running process's."""
    env = dict(os.environ) if env is None else env
    platform = sys.platform if platform is None else platform
    if env.get("QT_CACHE_DIR"):
        return Path(env["QT_CACHE_DIR"]).expanduser()
    home = Path(env.get("HOME") or Path.home())
    if platform == "darwin":
        return home / "Library" / "Caches" / "queuetruth"
    if platform.startswith("win"):
        base = env.get("LOCALAPPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / "queuetruth" / "Cache"
    xdg = env.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".cache") / "queuetruth"


__all__ = ["user_cache_dir"]
