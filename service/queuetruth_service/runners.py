"""Job runners. Child side (`replay`, `calibrate`, `score`, `report`, `ingest`): call the `queuetruth` Python API
(SPEC 4.3) with a spec whose references were resolved to paths relative to the job's work directory, and return the
summary document. Archive inputs are unpacked in the child too (`apply_extractions`), inside its memory limit,
timeout and cancel (security review M2). Parent side helpers: output listing and content types, usage meters.

Directory inputs (canonical live logs for `ScorecardSpec.live`, `sim.log`, `CalibrationSpec.live`) are uploaded as
`.zip`, `.tar`, `.tar.gz` or `.tgz` archives and extracted with guards (SPEC 8.10): at most `MAX_MEMBERS` entries of
any kind (each costs an inode), at most `MAX_INFLATED` bytes and a decompression ratio of `MAX_RATIO` (counting
`ENTRY_OVERHEAD` bytes per entry, so empty-member bombs count too), no absolute or `..` paths, no links, devices or
sparse members. The limits are checked while reading the member headers, before anything is written. Tar extraction
also uses `filter="data"` and is refused on Python versions whose data filter has known bypasses (L7:
CVE-2024-12718, CVE-2025-4138, CVE-2025-4330, CVE-2025-4435, CVE-2025-4517; fixed in 3.11.13, 3.12.11, 3.13.4)."""

from __future__ import annotations

import struct
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz")
MAX_RATIO = 200
MAX_INFLATED = 64 << 30
MAX_MEMBERS = 100_000
#: Cost of one entry in the ratio guard: a filesystem block per inode, so header-only bombs (thousands of empty
#: members at a few compressed bytes each) exceed the ratio long before `MAX_MEMBERS`.
ENTRY_OVERHEAD = 4096
#: First patched release of each minor version for the tarfile data-filter CVEs; later minors are all patched.
TAR_FILTER_FLOOR = {(3, 9): 23, (3, 10): 18, (3, 11): 13, (3, 12): 11, (3, 13): 4}
EXTRACT_TOKEN = "\x00qt-extract:"


class ArchiveError(Exception):
    """Unsafe or unreadable archive (`QT-INPUT-002`)."""

    code = "QT-INPUT-002"

    @property
    def message(self) -> str:
        """The error text (crosses the child process boundary)."""
        return str(self)


def tar_filter_patched(version: tuple[int, ...] | None = None) -> bool:
    """True when this interpreter's `tarfile` data filter has the 2025 path-escape fixes."""
    v = tuple((version or tuple(sys.version_info))[:3])
    floor = TAR_FILTER_FLOOR.get((v[0], v[1]))
    if floor is not None:
        return v[2] >= floor
    return (v[0], v[1]) > (3, 13)


# ---- child side -------------------------------------------------------------------------------------------------


def _dump(doc: Any) -> dict[str, Any]:
    return doc.model_dump(mode="json", by_alias=True) if hasattr(doc, "model_dump") else dict(doc)


def replay(spec: dict[str, Any]) -> dict[str, Any]:
    """`queuetruth.replay`."""
    from queuetruth import api

    return _dump(api.replay(spec))


def calibrate(spec: dict[str, Any]) -> dict[str, Any]:
    """`queuetruth.calibrate`."""
    from queuetruth import api

    return _dump(api.calibrate(spec))


def score(spec: dict[str, Any]) -> dict[str, Any]:
    """`queuetruth.score`."""
    from queuetruth import api

    return _dump(api.score(spec))


def report(spec: dict[str, Any]) -> dict[str, Any]:
    """`queuetruth.report`."""
    from queuetruth import api

    return _dump(api.report(spec))


def ingest(spec: dict[str, Any]) -> dict[str, Any]:
    """`queuetruth.ingest` (dataset validation jobs)."""
    from queuetruth import api

    return _dump(api.ingest(spec))


#: job kind -> child target.
TARGETS: dict[str, str] = {
    "replay": "queuetruth_service.runners:replay",
    "calibration": "queuetruth_service.runners:calibrate",
    "scorecard": "queuetruth_service.runners:score",
    "report": "queuetruth_service.runners:report",
    "dataset_validate": "queuetruth_service.runners:ingest",
    "ingest": "queuetruth_service.runners:ingest",
}

# ---- parent side ------------------------------------------------------------------------------------------------


def is_archive(name: str) -> bool:
    """True for supported archive file names."""
    return name.lower().endswith(ARCHIVE_SUFFIXES)


def _check_name(name: str) -> None:
    p = PurePosixPath(name)
    if not name or p.is_absolute() or ".." in p.parts or "\\" in name or "\x00" in name:
        raise ArchiveError(f"unsafe path in archive: {name[:200]!r}")


def _guard(inflated: int, members: int, compressed: int, max_members: int = MAX_MEMBERS) -> None:
    if members > max_members:
        raise ArchiveError(f"archive has more than {max_members} entries")
    cost = inflated + members * ENTRY_OVERHEAD
    if inflated > MAX_INFLATED or cost / compressed > MAX_RATIO:
        raise ArchiveError("archive exceeds the decompression guard (ratio 200, 64 GiB)")


def _zip_entry_count(path: Path) -> int | None:
    """Total entries from the end-of-central-directory record (read before the central directory is parsed)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - (1 << 16) - 22))
            tail = f.read()
        i = tail.rfind(b"PK\x05\x06")
        if i < 0 or len(tail) < i + 22:
            return None
        n = struct.unpack("<H", tail[i + 10 : i + 12])[0]
        return None if n == 0xFFFF else n  # zip64: counted from the parsed directory instead
    except OSError:
        return None


def _extract_zip(archive: Path, dest: Path, compressed: int, max_members: int) -> None:
    count = _zip_entry_count(archive)
    if count is not None:
        _guard(0, count, compressed, max_members)
    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        _guard(sum(i.file_size for i in infos), len(infos), compressed, max_members)
        for i in infos:
            _check_name(i.filename)
        z.extractall(dest)


def _extract_tar(archive: Path, dest: Path, compressed: int, max_members: int) -> None:
    if not tar_filter_patched():
        v = ".".join(str(x) for x in sys.version_info[:3])
        raise ArchiveError(
            f"tar extraction is disabled on Python {v}: its tarfile data filter has known path-escape bypasses; "
            "run the worker on Python >= 3.11.13, 3.12.11 or 3.13.4 (or upload a .zip)"
        )
    with tarfile.open(archive) as t:
        members = inflated = 0
        while (m := t.next()) is not None:  # headers only; stops at the first limit, before any write
            members += 1
            if m.isfile():
                inflated += m.size
            _guard(inflated, members, compressed, max_members)
            _check_name(m.name)
            if m.issym() or m.islnk() or m.ischr() or m.isblk() or m.isfifo() or m.issparse():
                raise ArchiveError(f"unsupported member type in archive: {m.name[:200]!r}")
            if not (m.isfile() or m.isdir()):
                raise ArchiveError(f"unsupported member type in archive: {m.name[:200]!r}")
        t.extractall(dest, filter="data")


def extract_archive(archive: Path, dest: Path, *, max_members: int = MAX_MEMBERS) -> Path:
    """Extracts safely into `dest`; returns the single top-level directory if there is exactly one, else `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    compressed = max(1, archive.stat().st_size)
    try:
        if archive.name.lower().endswith(".zip"):
            _extract_zip(archive, dest, compressed, max_members)
        else:
            _extract_tar(archive, dest, compressed, max_members)
    except ArchiveError:
        raise
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError, ValueError) as e:
        raise ArchiveError(f"unreadable archive: {type(e).__name__}") from e
    entries = [p for p in dest.iterdir() if not p.name.startswith(".") and p.name != "__MACOSX"]
    return entries[0] if len(entries) == 1 and entries[0].is_dir() else dest


def extraction_token(n: int) -> str:
    """Placeholder path of the n-th archive input (contains NUL, so it can never come from a stored spec)."""
    return f"{EXTRACT_TOKEN}{n}"


def apply_extractions(spec: Any, plan: list[dict[str, str]]) -> Any:
    """Child side: extracts each planned archive (paths relative to the cwd, the job directory) and replaces its
    token in `spec` with the extracted root, relative to the job directory."""
    roots: dict[str, str] = {}
    cwd = Path.cwd()
    for item in plan:
        try:
            root = extract_archive(Path(item["archive"]), Path(item["dest"]))
        except ArchiveError as e:
            raise ArchiveError(f"{item.get('label', 'archive')}: {e}") from e
        roots[item["token"]] = (root.relative_to(cwd) if root.is_absolute() else root).as_posix()

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            return roots.get(v, v)
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(spec)


CONTENT_TYPES = {
    ".parquet": "application/vnd.apache.parquet",
    ".json": "application/json",
    ".html": "text/html; charset=utf-8",
    ".csv": "text/csv",
    ".py": "text/x-python",
    ".npz": "application/octet-stream",
    ".txt": "text/plain; charset=utf-8",
}


def content_type(name: str) -> str:
    """Content type by file extension."""
    return CONTENT_TYPES.get(PurePosixPath(name).suffix.lower(), "application/octet-stream")


def list_outputs(out_dir: Path) -> list[tuple[str, Path]]:
    """`(relative posix name, path)` of every file under `out_dir`, sorted."""
    if not out_dir.exists():
        return []
    if out_dir.is_file():
        return [(out_dir.name, out_dir)]
    return sorted((p.relative_to(out_dir).as_posix(), p) for p in out_dir.rglob("*") if p.is_file())


def usage_meters(kind: str, summary: dict[str, Any] | None, cpu_ms: int) -> dict[str, tuple[float, str]]:
    """Meters of a succeeded job (SPEC 8.9): name -> (quantity, unit)."""
    s = summary or {}
    meters: dict[str, tuple[float, str]] = {"cpu_ms": (float(cpu_ms), "ms")}
    if kind == "replay":
        stats = s.get("stats") or {}
        meters["replay_records"] = (float(stats.get("records", 0) or 0), "records")
        shadows = s.get("shadows") or {}
        if shadows:
            meters["shadow_orders"] = (float(shadows.get("n", 0) or 0), "orders")
    elif kind == "calibration":
        n_shadows = float(s.get("n_shadows", 0) or 0)
        meters["shadow_orders"] = (n_shadows, "orders")
        models = len(s.get("held_out") or {}) + len(s.get("baseline_held_out") or {})
        if models:
            meters["zoo_pairs"] = (n_shadows * models, "pairs")
    elif kind == "scorecard":
        meters["orders_scored"] = (float(s.get("n_orders", 0) or 0), "orders")
    return meters
