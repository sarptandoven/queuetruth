"""Helpers of the end-to-end tests: repository paths, the installed `queuetruth` console script, the
`queuetruth-core` binary, output digests and JSON Schema validation of summary documents (O-SCHEMA-3)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SYNTH = REPO / "tests" / "fixtures" / "synth_small"
SCHEMA_DIR = REPO / "schemas" / "json"
PG_BIN_DEFAULT = "/opt/homebrew/opt/postgresql@16/bin"
TARGET = Path(os.environ.get("CARGO_TARGET_DIR", REPO / "target"))


def core_available() -> bool:
    """True when `queuetruth._core` is importable."""
    try:
        return importlib.util.find_spec("queuetruth._core") is not None
    except (ImportError, ValueError):
        return False


def postgres_available() -> bool:
    """True with `QT_PG_DSN` set or `initdb`/`pg_ctl` in `QT_PG_BIN`."""
    if os.environ.get("QT_PG_DSN"):
        return True
    b = Path(os.environ.get("QT_PG_BIN", PG_BIN_DEFAULT))
    return (b / "initdb").exists() and (b / "pg_ctl").exists()


def service_available() -> bool:
    """True when the service package is installed."""
    try:
        return importlib.util.find_spec("queuetruth_service") is not None
    except (ImportError, ValueError):
        return False


# ---- helpers ------------------------------------------------------------------------------------------------------


def cli_path() -> Path:
    """The installed `queuetruth` console script of this interpreter's environment."""
    p = Path(sys.executable).parent / "queuetruth"
    if not p.exists():
        found = shutil.which("queuetruth")
        if found is None:
            pytest.skip("the `queuetruth` console script is not installed (make develop)")
        p = Path(found)
    return p


def core_binary() -> Path | None:
    """`queuetruth-core` from the shared target dir (release preferred, newest of the two otherwise)."""
    cands = [TARGET / "release" / "queuetruth-core", TARGET / "debug" / "queuetruth-core"]
    have = [c for c in cands if c.exists()]
    return max(have, key=lambda c: c.stat().st_mtime) if have else None


def run_cli(
    *args: str,
    binary: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    cwd: Path | None = None,
) -> Any:
    """Runs `queuetruth --format json ARGS` (or another binary with the same grammar); returns the parsed stdout."""
    exe = binary or cli_path()
    cmd = [str(exe), "--format", "json", *args]
    p = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, **(env or {})}, timeout=900, cwd=cwd)
    if check and p.returncode != 0:
        raise AssertionError(f"{' '.join(cmd)} -> exit {p.returncode}\nstdout: {p.stdout[-2000:]}\n{p.stderr[-4000:]}")
    return json.loads(p.stdout) if p.stdout.strip() else None


def strip_volatile(doc: Any) -> Any:
    """Drops `timings` (SPEC 7.2: JSON excluding timings) at any depth."""
    if isinstance(doc, dict):
        return {k: strip_volatile(v) for k, v in doc.items() if k != "timings"}
    if isinstance(doc, list):
        return [strip_volatile(v) for v in doc]
    return doc


def strip_paths(doc: Any) -> Any:
    """Also drops path-bearing fields (`spec` echo, `files` paths) for comparisons across output directories."""
    if isinstance(doc, dict):
        out = {}
        for k, v in doc.items():
            if k in ("timings", "spec"):
                continue
            if k == "files" and isinstance(v, list):
                out[k] = sorted(
                    (strip_paths({kk: vv for kk, vv in f.items() if kk != "path"}) for f in v if isinstance(f, dict)),
                    key=lambda d: json.dumps(d, sort_keys=True),
                )
                continue
            if k == "path" and isinstance(v, str):
                continue
            out[k] = strip_paths(v)
        return out
    if isinstance(doc, list):
        return [strip_paths(v) for v in doc]
    return doc


def tree_digests(root: Path, skip: Iterable[str] = ()) -> dict[str, str]:
    """sha256 of every file under `root` (relative path -> hex); JSON files hashed without `timings`."""
    skip = set(skip)
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel in skip or p.name in skip:
            continue
        data = p.read_bytes()
        if p.suffix == ".json":
            try:
                data = json.dumps(strip_volatile(json.loads(data)), sort_keys=True).encode()
            except ValueError:
                pass
        out[rel] = hashlib.sha256(data).hexdigest()
    return out


_VALIDATORS: dict[str, Any] = {}


def schema_validate(doc: Any, name: str | None = None) -> str:
    """Validates a JSON document against `schemas/json/<schema>.schema.json` (O-SCHEMA-3); returns the schema name."""
    import jsonschema

    name = name or (doc.get("schema") if isinstance(doc, dict) else None)
    assert isinstance(name, str) and name.startswith("qt."), f"document has no qt.* schema field: {str(doc)[:200]}"
    v = _VALIDATORS.get(name)
    if v is None:
        path = SCHEMA_DIR / f"{name}.schema.json"
        assert path.exists(), f"no JSON Schema for {name}"
        schema = json.loads(path.read_text())
        cls = jsonschema.validators.validator_for(schema)
        cls.check_schema(schema)
        v = _VALIDATORS[name] = cls(schema)
    errs = sorted(v.iter_errors(doc), key=lambda e: list(e.path))
    assert not errs, f"{name}: " + "; ".join(f"{list(e.path)}: {e.message[:200]}" for e in errs[:5])
    return name


# ---- the synthetic pipeline through the installed CLI ---------------------------------------------------------------

REFEREE_FIXTURE = REPO / "tests" / "fixtures" / "referee" / "02_level_shrink_caps_queue"


EXPECTED_EXIT = {"export_hbt_refused": 1, "export_nt_refused": 1}


def pipeline_steps(threads: int) -> list[tuple[str, list[str]]]:
    """(stage, argv) of ingest -> replay -> zoo -> calibrate -> features -> fit-hazard -> score -> referee ->
    report -> export, all relative to the run directory so spec echoes are equal across run directories."""
    t = ["--threads", str(threads)]
    live = str(SYNTH / "own_orders" / "live")
    return [
        ("ingest", ["ingest", "--format", "parquet", "--in", str(SYNTH), "--venue", "cme", "--out", "ds"]),
        ("shadows", ["shadows", "sample", "--data", "ds", "--n", "160", "--out", "shadows.parquet", "--seed", "3",
                     "--lifetime", "60s"]),
        ("replay", ["replay", "--data", "ds", "--out", "replay", "--shadows", "shadows.parquet", "--rank-oracle",
                    "--parity-trades", *t]),
        ("zoo", ["zoo", "--data", "ds", "--shadows", "shadows.parquet", "--out", "zoo", "--models", "all",
                 "--bootstrap-b", "20", "--h-max", "1m", *t]),
        ("calibrate", ["calibrate", "alloc", "--family", "power3", "--data", "ds", "--shadows", "shadows.parquet",
                       "--out", "cal", "--train", "2026-06-15", "--test", "2026-06-16", "--bootstrap-b", "10", *t]),
        ("features", ["features", "--data", "ds", "--shadows", "shadows.parquet", "--out", "feat", *t]),
        ("fit_hazard", ["fit-hazard", "--features", "feat", "--out", "model", "--bootstrap-b", "20", *t]),
        ("score", ["score", "--live", live, "--sim", "exact", "--data", "ds", "--out", "score", "--bootstrap-b", "50",
                   "--perm-b", "50", *t]),
        ("referee", ["referee", "run", "--data", str(REFEREE_FIXTURE / "dataset"), "--schedule",
                     str(REFEREE_FIXTURE / "schedule.parquet"), "--sim-fills",
                     str(REFEREE_FIXTURE / "expected_fills.parquet"), "--out", "referee"]),
        ("report", ["report", "--in", "zoo", "--in", "cal", "--in", "score", "--in", "model", "--out",
                    "report/report.html", "--max-why", "2"]),
        # synthetic data: the fitted n does not beat the default out of sample, so export refuses (QT-CALIB-001,
        # exit 1) and writes nothing; --force writes the snippet under a NOT VALIDATED header
        ("export_hbt_refused", ["export", "hftbacktest", "--calibration", "cal/calibration.json", "--out",
                                "export/hbt_refused.py"]),
        ("export_hbt", ["export", "hftbacktest", "--calibration", "cal/calibration.json", "--out", "export/hbt.py",
                        "--force"]),
        ("export_nt_refused", ["export", "nautilus", "--calibration", "cal/calibration.json", "--out",
                               "export/nautilus_refused.py"]),
        ("export_nt", ["export", "nautilus", "--calibration", "cal/calibration.json", "--out", "export/nautilus.py",
                       "--force"]),
    ]  # fmt: skip


def run_pipeline(workdir: Path, threads: int = 1, omp: int = 1) -> dict[str, Any]:
    """Runs every pipeline step in `workdir` with the installed console script; returns stage -> stdout doc."""
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "report").mkdir(exist_ok=True)
    (workdir / "export").mkdir(exist_ok=True)
    env = {**os.environ, "OMP_NUM_THREADS": str(omp)}
    docs: dict[str, Any] = {}
    exe = cli_path()
    for stage, argv in pipeline_steps(threads):
        cmd = [str(exe), "--format", "json", *argv]
        p = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, env=env, timeout=1800)
        if p.returncode != EXPECTED_EXIT.get(stage, 0):
            raise AssertionError(
                f"{stage}: {' '.join(argv)} -> exit {p.returncode}\n{p.stdout[-2000:]}\n{p.stderr[-4000:]}"
            )
        docs[stage] = json.loads(p.stdout) if p.stdout.strip() else None
    return docs
