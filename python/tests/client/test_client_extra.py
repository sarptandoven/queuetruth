"""`queuetruth.client` without the `client` extra: importing it raises an ImportError that names the extra (release
stranger test), simulated with an import hook that hides httpx."""

from __future__ import annotations

import importlib
import importlib.abc
import sys

import pytest


class _HideHttpx(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        if fullname == "httpx" or fullname.startswith("httpx."):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


def _drop(prefixes: tuple[str, ...]) -> dict[str, object]:
    saved = {}
    for name in list(sys.modules):
        if any(name == p or name.startswith(p + ".") for p in prefixes):
            saved[name] = sys.modules.pop(name)
    return saved


def test_import_without_httpx_names_the_extra():
    prefixes = ("httpx", "queuetruth.client")
    saved = _drop(prefixes)
    finder = _HideHttpx()
    sys.meta_path.insert(0, finder)
    try:
        with pytest.raises(ImportError) as info:
            importlib.import_module("queuetruth.client")
        assert not isinstance(info.value, ModuleNotFoundError) and info.value.name == "httpx"
        assert 'pip install "queuetruth[client]"' in str(info.value)
        assert isinstance(info.value.__cause__, ModuleNotFoundError) and info.value.__cause__.name == "httpx"
        with pytest.raises(ImportError, match=r"queuetruth\[client\]"):
            importlib.import_module("queuetruth.client.testing")
    finally:
        sys.meta_path.remove(finder)
        _drop(prefixes)
        sys.modules.update(saved)
    import queuetruth.client

    assert queuetruth.client.Client is not None  # restored for the other tests


def test_import_with_httpx_works():
    import queuetruth.client

    assert "Client" in queuetruth.client.__all__
