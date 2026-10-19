"""Real DBN bytes (opt-in): Databento's Apache-2.0 MBO test files, fetched by `scripts/realdata/dbn_samples.py` into
`.realdata/dbn-samples/` (never committed). Every encoding (DBN v1, v2, v3, zstd and plain) decodes to the same rows,
ingest and replay run, and the only non-zero invariant is the expected unknown-order count of a mid-stream excerpt.
Skipped when the files were not fetched."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from e2e_support import REPO

pytestmark = [pytest.mark.realdata, pytest.mark.requires_core]
SAMPLES = REPO / ".realdata" / "dbn-samples"


def test_databento_dbn_samples_decode_and_replay(tmp_path) -> None:
    if not (SAMPLES / "test_data.mbo.v3.dbn").exists():
        pytest.skip("run scripts/realdata/dbn_samples.py first (downloads Databento's DBN test files)")
    for f in SAMPLES.glob("test_data.mbo*"):
        (tmp_path / f.name).write_bytes(f.read_bytes())
    p = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "realdata" / "dbn_samples.py"), "--offline", "--out", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    doc = json.loads((tmp_path / "results.json").read_text())
    assert p.returncode == 0, (p.stdout, p.stderr[-2000:], doc)
    assert doc["all_encodings_decode_identically"] is True
    assert all(r["status"] == "pass" and r["sha256_ok"] for r in doc["files"].values())
