"""SPEC 9 data guardrail (the `check_no_market_data` gate): no market data in the repository.

1. No feed-capture file types anywhere (ITCH, DBN, pcap, zstd, gzip) outside build/venv/real-data directories; gzip
   only inside a manifest-listed synthetic fixture directory.
2. Data-like files (Parquet, CSV, JSONL, npz) live only under fixture directories and are small.
3. Every file of a `tests/fixtures/<dir>` with a `MANIFEST.sha256` is listed there with a matching sha256.
4. `.realdata/` and `.scratch/` are ignored by git.
5. No line of a repository JSONL/CSV fixture equals a message line of the real crypto captures of the author's earlier
   et prototype under `$QT_ET_ROOT/data` (skipped when QT_ET_ROOT is unset or those captures are absent)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from e2e_support import REPO

# `.queuetruth` is the trial ledger's runtime directory (queuetruth.ledger): it writes its own `*` .gitignore, so
# nothing in it can be committed, and it records runs made in this checkout (make examples, the CLI).
SKIP_DIRS = {
    "target",
    ".venv",
    ".realdata",
    ".scratch",
    ".git",
    ".hypothesis",
    ".pytest_cache",
    ".ruff_cache",
    "site",
    ".queuetruth",
}
FORBIDDEN = (".itch", ".dbn", ".zst", ".pcap", ".pcapng", ".gz", ".tgz", ".bz2", ".xz")
DATA_LIKE = (".parquet", ".csv", ".jsonl", ".npz", ".arrow", ".feather")
FIXTURE_ROOTS = ("tests/fixtures/", "schemas/samples/")
MAX_DATA_FILE = 3 * 1024 * 1024
#: Real crypto captures of the et prototype (`$QT_ET_ROOT/data`); unset means the check is skipped.
ET_CAPTURES = Path(os.environ["QT_ET_ROOT"]) / "data" if os.environ.get("QT_ET_ROOT") else None


def repo_files() -> list[Path]:
    out = []
    stack = [REPO]
    while stack:
        d = stack.pop()
        for p in d.iterdir():
            if p.is_dir():
                if p.name not in SKIP_DIRS and not p.name.endswith(".egg-info") and p.name != "__pycache__":
                    stack.append(p)
            elif p.is_file():
                out.append(p)
    return sorted(out)


def _is_fixture(rel: str) -> bool:
    return rel.startswith(FIXTURE_ROOTS) or "/tests/fixtures/" in f"/{rel}" or "/fixtures/" in rel


def _manifested(p: Path) -> bool:
    """Inside a `tests/fixtures/<dir>` that has a MANIFEST.sha256 (its listing is checked below)."""
    rel = p.relative_to(REPO).parts
    return (
        len(rel) > 3
        and rel[:2] == ("tests", "fixtures")
        and (REPO / "tests" / "fixtures" / rel[2] / "MANIFEST.sha256").exists()
    )


def test_no_feed_capture_files() -> None:
    bad = [
        p.relative_to(REPO).as_posix()
        for p in repo_files()
        if p.name.lower().endswith(FORBIDDEN) and not (p.name.lower().endswith(".gz") and _manifested(p))
    ]
    assert not bad, bad


def test_data_like_files_only_in_fixture_dirs_and_small() -> None:
    misplaced, big = [], []
    for p in repo_files():
        if not p.name.lower().endswith(DATA_LIKE):
            continue
        rel = p.relative_to(REPO).as_posix()
        if not _is_fixture(rel):
            misplaced.append(rel)
        if p.stat().st_size > MAX_DATA_FILE:
            big.append((rel, p.stat().st_size))
    assert not misplaced, misplaced
    assert not big, big


@pytest.mark.parametrize(
    "manifest", sorted((REPO / "tests" / "fixtures").glob("*/MANIFEST.sha256")), ids=lambda p: p.parent.name
)
def test_fixture_manifest_complete_and_valid(manifest: Path) -> None:
    root = manifest.parent
    listed: dict[str, str] = {}
    for line in manifest.read_text().splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            listed[name.lstrip("*").removeprefix("./")] = digest
    present = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.name != "MANIFEST.sha256" and "__pycache__" not in p.parts and p.name != ".DS_Store"
    }
    assert present - set(listed) == set(), f"unlisted files in {root.name}: {sorted(present - set(listed))[:10]}"
    assert set(listed) - present == set(), f"listed but missing in {root.name}: {sorted(set(listed) - present)[:10]}"
    wrong = [n for n, d in listed.items() if hashlib.sha256((root / n).read_bytes()).hexdigest() != d]
    assert not wrong, wrong


def test_realdata_and_scratch_are_gitignored() -> None:
    lines = {x.strip().rstrip("/") for x in (REPO / ".gitignore").read_text().splitlines()}
    assert {".realdata", ".scratch", "target", ".venv"} <= {x.lstrip("/") for x in lines}


def test_no_line_copied_from_real_crypto_captures() -> None:
    caps = sorted(p for p in ET_CAPTURES.rglob("*") if p.is_file()) if ET_CAPTURES and ET_CAPTURES.exists() else []
    if not caps:
        pytest.skip("et real captures not present (set QT_ET_ROOT)")
    ours: dict[str, str] = {}
    for p in repo_files():
        if p.name.lower().endswith((".jsonl", ".csv", ".json")) and _is_fixture(p.relative_to(REPO).as_posix()):
            for line in p.read_bytes().splitlines():
                line = line.strip()
                if len(line) >= 40:  # headers and tiny rows are not evidence
                    ours[hashlib.sha256(line).hexdigest()] = p.relative_to(REPO).as_posix()
    hits = []
    for cap in caps:
        with cap.open("rb") as f:
            for line in f:
                h = hashlib.sha256(line.strip()).hexdigest()
                if h in ours:
                    hits.append((ours[h], cap.relative_to(ET_CAPTURES).as_posix()))
                    if len(hits) > 5:
                        break
    assert not hits, hits
