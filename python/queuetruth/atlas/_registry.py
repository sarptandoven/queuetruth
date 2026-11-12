"""Finds the document models of the atlas data API: every public module of `queuetruth.atlas` that defines
`DOC_MODELS` (stdlib only; `queuetruth.schemas` calls it once every schema module is complete)."""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any


def doc_models() -> dict[str, Any]:
    """{schema name: model} of every public module of the package, sorted by schema name."""
    import queuetruth.atlas as pkg

    out: dict[str, Any] = {}
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if info.name.startswith("_") or info.ispkg:
            continue
        mod = importlib.import_module(f"{pkg.__name__}.{info.name}")
        for name, model in (getattr(mod, "DOC_MODELS", None) or {}).items():
            if name in out:
                raise RuntimeError(f"atlas: schema {name} defined twice")
            out[name] = model
    return dict(sorted(out.items()))


__all__ = ["doc_models"]
