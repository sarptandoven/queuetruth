"""Committed schema artifacts of the prediction-market kit, in this directory.

- `<name>.schema.json`: JSON Schema (draft 2020-12) of each `queuetruth pm` document (`qt.pm_ingest_summary`,
  `qt.pm_calibration`, `qt.pm_score`) and spec (`qt.pm_ingest_spec`, `qt.pm_calibration_spec`,
  `qt.pm_score_spec`), from the Pydantic models of `queuetruth.pm.models`.
- `<table>.arrow.json`: dump of each Arrow table schema of `queuetruth.pm.tables` (the layout of
  `schemas/arrow/*.json`).

They are Python-only: the Rust core embeds `schemas/json/` and does not know these documents. `export()` rewrites
the files; `python/tests/pm/test_schemas_and_fixtures.py` fails when they are stale. Regenerate with
`python -c "from queuetruth.pm.schemas import export; export()"`.
"""

from __future__ import annotations

import json
from pathlib import Path

from queuetruth.pm.models import DOC_MODELS, SPEC_MODELS
from queuetruth.pm.tables import PM_SCHEMAS
from queuetruth.schemas.arrow import schema_dump
from queuetruth.schemas.export import json_schema

#: The directory holding the committed artifacts.
HERE = Path(__file__).resolve().parent


def _text(obj: object) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def artifacts() -> dict[str, str]:
    """File name -> exact text of every artifact (sorted by name)."""
    out: dict[str, str] = {}
    for name, model in sorted({**DOC_MODELS, **SPEC_MODELS}.items()):
        out[f"{name}.schema.json"] = _text(json_schema(name, model))
    for name, sch in sorted(PM_SCHEMAS.items()):
        out[f"{name}.arrow.json"] = _text(schema_dump(name, sch))
    return dict(sorted(out.items()))


def load(name: str) -> dict[str, object]:
    """A committed JSON Schema by document or spec name (e.g. `qt.pm_score`)."""
    path = HERE / f"{name}.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def export(root: str | Path | None = None) -> list[Path]:
    """Writes every artifact into `root` (default: this directory); returns the paths written."""
    d = Path(root) if root is not None else HERE
    d.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in artifacts().items():
        p = d / name
        p.write_text(text, encoding="utf-8")
        written.append(p)
    return written


__all__ = ["HERE", "artifacts", "export", "load"]
