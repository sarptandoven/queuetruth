"""Run ids and seed derivation have one implementation, in Rust (`qt_schema::run_id`, `qt_types::stable_seed`),
called through `queuetruth._core`: the Python names keep their signatures, errors and bytes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from queuetruth import schemas
from queuetruth.schemas import runid
from queuetruth.stats import bootstrap_seed, splitmix64, stable_seed

pytestmark = pytest.mark.requires_core

REPO = Path(__file__).resolve().parents[3]
KAT = json.loads((REPO / "schemas" / "run_id_kat.json").read_text())

_MASK = (1 << 64) - 1


def _stable_seed_before(run_seed: int, parts: list[bytes | str]) -> int:
    """The pure-Python derivation the package had before it called `_core` (reference only)."""
    h = 0xCBF29CE484222325

    def feed(data: bytes) -> None:
        nonlocal h
        for b in data:
            h = ((h ^ b) * 0x100000001B3) & _MASK

    feed(run_seed.to_bytes(8, "little"))
    for p in parts:
        raw = p.encode("utf-8") if isinstance(p, str) else bytes(p)
        feed(min(len(raw), 0xFFFFFFFF).to_bytes(4, "little"))
        feed(raw)
    z = (h + 0x9E3779B97F4A7C15) & _MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
    return z ^ (z >> 31)


def test_seeds_equal_the_python_derivation() -> None:
    for seed in (0, 1, 42, 2**63, _MASK):
        for parts in ([], [b"bootstrap", "fill_rate"], ["é\U0001f600", b"\x00" * 300], [""]):
            assert stable_seed(seed, parts) == _stable_seed_before(seed, parts)
        assert bootstrap_seed(seed, "t") == _stable_seed_before(seed, [b"bootstrap", "t"])
    assert splitmix64(-1) == splitmix64(_MASK)
    with pytest.raises(ValueError):
        stable_seed(-1, [])


def test_run_id_calls_resolve_once_per_ref_in_document_order() -> None:
    spec = {
        "dataset": {"path": "d"},
        "out": {"path": "ignored: execution-only"},
        "shadows": {"upload_id": "u"},
        "inputs": [{"job_file": {"job_id": "j", "name": "n"}}, {"path": "d"}],
    }
    seen: list[dict] = []

    def resolve(r: dict) -> str:
        seen.append(r)
        return f"{len(seen):064x}"

    doc = runid.identity_document(spec, "0.1.0", resolve)
    assert [next(iter(r)) for r in seen] == ["path", "upload_id", "job_file", "path"]
    assert '"out"' not in doc
    assert runid.run_id(spec, "0.1.0", lambda r: "ab" * 32) == runid.run_id(spec, "0.1.0", lambda r: "ab" * 32)


def test_run_id_known_answers_and_errors() -> None:
    for case in KAT["cases"]:
        refs = case["refs"]
        got = runid.run_id(
            case["spec"],
            case["engine_version"],
            lambda r, refs=refs: refs[json.dumps(r, separators=(",", ":"), sort_keys=True)],
        )
        assert got == case["run_id"], case["name"]
    with pytest.raises(TypeError):
        runid.run_id({1: 2}, "0.1.0", lambda r: "")
    with pytest.raises(TypeError):
        runid.run_id({"a": object()}, "0.1.0", lambda r: "")
    with pytest.raises(ValueError):
        runid.run_id({"a": float("nan")}, "0.1.0", lambda r: "")
    with pytest.raises(ValueError):
        runid.run_id({"a": 2**70}, "0.1.0", lambda r: "")


def test_v1_1_defaults_come_from_rust() -> None:
    table = schemas.V1_1_DEFAULTS
    assert table is runid.V1_1_DEFAULTS
    assert ("sweep_gap_ns", ("fill_rule",), "exact") in table
    assert ("window_open_s", ("partition_quotas",), {}) in table
    spec = {"sweep_gap_ns": 1, "fill_rule": "exact", "exclude_order_ids": [{"order_id": 1, "session_date": None}]}
    assert runid.drop_v1_1_defaults(spec) == {"sweep_gap_ns": 1, "exclude_order_ids": [{"order_id": 1}]}
