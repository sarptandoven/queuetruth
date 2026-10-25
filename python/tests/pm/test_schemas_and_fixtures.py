"""Committed artifacts stay current: the kit's JSON Schemas and Arrow dumps, and the generated fixtures; every
document the pipeline writes validates against its schema; the kit has no network code."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import pyarrow.parquet as pq
import pytest
from pm._helpers import FIX, REPO

import queuetruth.pm as pm_pkg
from queuetruth.pm import schemas
from queuetruth.pm.fixtures import all_files
from queuetruth.pm.models import DOC_MODELS, SPEC_MODELS
from queuetruth.pm.tables import PM_SCHEMAS
from queuetruth.pm.terms import SOURCES, TERMS_NOTICE


def test_committed_schema_artifacts_are_current() -> None:
    want = schemas.artifacts()
    have = {p.name: p.read_text(encoding="utf-8") for p in schemas.HERE.glob("*.json")}
    assert set(have) == set(want), "run: python -c 'from queuetruth.pm.schemas import export; export()'"
    for name, text in want.items():
        assert have[name] == text, f"{name} is stale"
    assert len(want) == len(DOC_MODELS) + len(SPEC_MODELS) + len(PM_SCHEMAS)


@pytest.mark.parametrize("venue", ["kalshi", "polymarket"])
def test_written_documents_validate_and_tables_carry_their_schema(venue: str, kalshi_run: Any, poly_run: Any) -> None:
    run = kalshi_run if venue == "kalshi" else poly_run
    for rel, name in (
        ("in/pm_ingest.json", "qt.pm_ingest_summary"),
        ("cal/pm_calibration.json", "qt.pm_calibration"),
        ("sc/pm_score.json", "qt.pm_score"),
    ):
        doc = json.loads((run.root / rel).read_text(encoding="utf-8"))
        jsonschema.validate(doc, schemas.load(name))
        assert doc["schema"] == name and doc["terms"] == TERMS_NOTICE and re.fullmatch("[0-9a-f]{16}", doc["run_id"])
    for f in [*run.root.glob("in/pm/*.parquet"), *run.root.glob("sc/*.parquet")]:
        meta = pq.read_schema(f).metadata
        name = meta[b"qt.schema"].decode()
        assert pq.read_schema(f).equals(PM_SCHEMAS[name], check_metadata=False), f.name


def test_fixtures_are_reproducible_and_listed_in_the_manifest() -> None:
    files = all_files()
    on_disk = {str(p.relative_to(FIX)): p.read_bytes() for p in FIX.rglob("*") if p.is_file()}
    assert set(on_disk) == set(files)
    for rel, data in files.items():
        assert on_disk[rel] == data, f"{rel} differs from a fresh generation"
        assert len(data) < 3_000_000
    lines = (FIX / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines()
    listed = dict(reversed(line.split("  ", 1)) for line in lines)
    assert set(listed) == set(files) - {"MANIFEST.sha256"}
    for rel, digest in listed.items():
        assert hashlib.sha256(on_disk[rel]).hexdigest() == digest


def test_fixtures_name_no_real_market_or_account() -> None:
    text = "".join(p.read_text(encoding="utf-8") for p in FIX.rglob("*") if p.suffix in (".json", ".jsonl", ".csv"))
    assert "KXDEMO" in text and "demo" in text
    assert "demo-order" not in text  # order hashes are hashed, never written as text


def test_the_kit_reads_files_only() -> None:
    """No network code anywhere in the package (the terms note relies on it)."""
    pkg = Path(pm_pkg.__file__).parent
    pattern = re.compile(r"^\s*(import|from)\s+(urllib|http|socket|requests|httpx|aiohttp|websocket)", re.M)
    for f in pkg.rglob("*.py"):
        assert not pattern.search(f.read_text(encoding="utf-8")), f
    from queuetruth.pm import fees, rewards

    for table in (SOURCES, fees.SOURCES, rewards.SOURCES):  # every formula and statement names its source and date
        assert table and all(u.startswith("https://") and d.startswith("2026-10-") and w for u, w, d in table)
    assert (REPO / "tests" / "fixtures" / "pm" / "README.txt").read_text().startswith("Prediction-market fixtures")
