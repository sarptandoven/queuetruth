"""Prints the OpenAPI 3.1 document (`make openapi` writes it to `service/openapi.json`)."""

from __future__ import annotations

import json
import sys
import tempfile

from queuetruth_service.settings import Settings
from queuetruth_service.store.local import LocalStore


def document() -> dict:
    """The OpenAPI document of an app built with default settings (no database connection)."""
    from queuetruth_service.app import create_app

    with tempfile.TemporaryDirectory() as tmp:
        s = Settings(_env_file=None, store="local", store_root=tmp)
        app = create_app(s, store=LocalStore(tmp, s.signing_secret), connect=False)
        return app.openapi()


def render() -> str:
    """Deterministic JSON text of the document."""
    return json.dumps(document(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    """Writes the document to stdout."""
    sys.stdout.write(render())


if __name__ == "__main__":
    main()
