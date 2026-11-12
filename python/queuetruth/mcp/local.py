"""Local access for the MCP local tools (SPEC 4.6, tools marked L): read-only output directories, and (opt-in, run
mode) sandboxed local runs of the operations with an MCP tool (`queuetruth.ops.table`: shadow sampling, replay, zoo
and the verbs) and of why, on local datasets without the service. The child calls `queuetruth.runner.run`; the path
containment is the runner's (`queuetruth.runner.Roots`).

Read tools.

Every path argument is resolved against the root (`QT_MCP_ROOT`, default the working directory); after resolving
symlinks (`os.path.realpath`) it must stay inside the root, else `QT-CONFIG-001`. The same holds for every path found
*inside* a directory argument: summary candidates, listed documents and the files of a why-trace dataset are resolved
one by one, and a child that is a symlink to somewhere outside the root is refused (security review L3). Files are
opened through their resolved path. Nothing here writes: files are opened read-only and `_core.why` runs with
`out = null`. A summary is any JSON file whose top-level `schema` names a `qt.*` document of
SPEC 3.10; it is validated with the shared Pydantic model before it is returned unchanged.

Run mode (`QT_MCP_ALLOW_RUN=1` or `--allow-run`; `LocalRunner`; POSIX only). Inputs are `{"path"}` references
only; each is resolved with realpath and must lie inside an allowed input root (`QT_MCP_ROOT`, every directory of
`QT_MCP_DATA_ROOTS` (os.pathsep-separated) and the run root). No allowed root may be the filesystem root, the home
directory or a parent of it (`QT-CONFIG-001` at startup: a stdio server started from `/` without `QT_MCP_ROOT` would
otherwise allow every file). A dataset must hold `manifest.json` and no symlink leading out of the roots (the check
walks at most `MAX_DATASET_ENTRIES` entries). Service references (`dataset_id`, `upload_id`, `job_file`) and a
non-null `out` are refused (`QT-CONFIG-001`). Outputs go to `<run root>/<kind>/<run_id>` (`QT_MCP_RUN_ROOT`, default
`<QT_MCP_ROOT>/qt-mcp-runs`, created at startup): the run writes into a private staging directory under
`<run root>/.staging` that is renamed into place only when the run succeeded within the limits, so a finished
directory is complete. `run_id` hashes the inputs' contents (SPEC 7.3), so an existing finished directory is the
answer for the same spec and is returned without running again; a directory at that place whose `summary.json` does
not validate is removed before the run. Identical specs in one server run once (the second waits for the first).

Every run executes in a fresh `python -I` child (its own session, so the whole process group is killed on timeout,
on cancel and on a limit) with a minimal environment (no `QT_*` settings, no API key; `HOME`/`TMPDIR` in a private
directory; `RAYON_NUM_THREADS`/`OMP_NUM_THREADS` = the spec's `threads`) and an audit hook refusing unpickling.
`RunLimits` (env `QT_MCP_RUN_*`): wall-clock timeout, CPU seconds (`RLIMIT_CPU`), output bytes (`RLIMIT_FSIZE` per
file, at most the free disk above the floor; CPython ignores SIGXFSZ, so the write fails with EFBIG; the run's total
and the free disk are polled while it runs), memory (a watchdog reads the child's physical footprint on macOS, which
unlike RSS counts compressed pages, `VmRSS` from `/proc` on Linux, else RSS from `ps`), free disk on the run root, the
compute knobs (threads, particles, bootstrap draws, zoo models and horizons), shadow counts (from the Parquet footer,
or the sampling spec's exact quota sum over the manifest's partitions) and concurrent runs (excess calls wait). The
watchdog polls every `WATCH_POLL_S`, so a run can overshoot the memory and output limits by what it allocates or
writes in one interval.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from queuetruth.dataset import read_manifest_doc
from queuetruth.errors import QtError
from queuetruth.ops.table import OperationDef, table
from queuetruth.runner import MAX_TREE_ENTRIES, Roots, check_dataset_tree
from queuetruth.schemas import specs as sp
from queuetruth.schemas.models import DOC_MODELS, parse_document

ENV_ROOT = "QT_MCP_ROOT"
MAX_JSON_BYTES = 64 << 20
MAX_DEPTH = 6
MAX_SCANNED_FILES = 20_000
MAX_ENTRIES = 2_000
#: Most entries the symlink check of one dataset directory walks (`QT-INPUT-002` beyond; `queuetruth.runner`).
MAX_DATASET_ENTRIES = MAX_TREE_ENTRIES
PREFERRED_SUMMARIES = ("summary.json", "calibration.json", "model.json", "report.json", "manifest.json")
_SCHEMA_PREFIX = re.compile(rb'^\s*\{\s*"schema"\s*:\s*"(qt\.[a-z0-9_]+)"')


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-JSON constant {name}")


class LocalOutputs:
    """Resolves and reads output directories under one root."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        base = root if root is not None else os.environ.get(ENV_ROOT) or os.getcwd()
        self.root = Path(base).expanduser().resolve()

    def resolve(self, path: str) -> Path:
        """Absolute path of `path` (relative paths are taken from the root); must stay inside the root."""
        if not isinstance(path, str) or not path or "\x00" in path:
            raise QtError("QT-USAGE-001", "path must be a non-empty string")
        p = Path(path).expanduser()
        full = self.inside(p if p.is_absolute() else self.root / p)
        if full is None:
            raise QtError("QT-CONFIG-001", f"path {path!r} is outside {ENV_ROOT} ({self.root})")
        return full

    def inside(self, path: Path) -> Path | None:
        """The real path of `path` (every symlink resolved) when it lies inside the root, else None."""
        real = Path(os.path.realpath(path))
        return real if real == self.root or self.root in real.parents else None

    def contain(self, path: Path) -> Path:
        """`inside`, raising `QT-CONFIG-001` for a path (typically a symlinked child) that leaves the root."""
        real = self.inside(path)
        if real is None:
            raise QtError("QT-CONFIG-001", f"{path.name!r} resolves outside {ENV_ROOT} ({self.root})")
        return real

    def rel(self, path: Path) -> str:
        """`path` relative to the root, POSIX separators."""
        return path.relative_to(self.root).as_posix() or "."

    @staticmethod
    def sniff_schema(path: Path) -> str | None:
        """The `schema` of a JSON document when it is the first key (cheap prefix read), else None."""
        try:
            with open(path, "rb") as f:
                head = f.read(4096)
        except OSError:
            return None
        match = _SCHEMA_PREFIX.match(head)
        if match is None:
            return None
        name = match.group(1).decode()
        return name if name in DOC_MODELS else None

    def load_document(self, path: Path) -> dict[str, Any]:
        """Parses and validates one `qt.*` JSON document under the root; returns it unchanged."""
        path = self.contain(path)
        if not path.is_file():
            raise QtError("QT-INPUT-001", f"{self.rel(path)}: not a file")
        if path.stat().st_size > MAX_JSON_BYTES:
            raise QtError("QT-INPUT-002", f"{self.rel(path)}: larger than {MAX_JSON_BYTES} bytes")
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f, parse_constant=_reject_constant)
        except (OSError, UnicodeDecodeError, ValueError) as e:
            raise QtError("QT-INPUT-002", f"{self.rel(path)}: not valid JSON: {e}") from e
        if not isinstance(doc, dict) or doc.get("schema") not in DOC_MODELS:
            raise QtError("QT-INPUT-002", f"{self.rel(path)}: not a qt.* document (missing or unknown `schema`)")
        try:
            parse_document(doc)
        except ValidationError as e:
            err = e.errors()[0]
            loc = ".".join(str(x) for x in err.get("loc", ()))
            raise QtError("QT-INPUT-002", f"{self.rel(path)}: invalid {doc['schema']}: {loc}: {err['msg']}") from e
        return doc

    def read_summary(self, path: str) -> dict[str, Any]:
        """A summary file, or the summary of an output directory (`summary.json` first, else the only document)."""
        full = self.resolve(path)
        if full.is_dir():
            for name in PREFERRED_SUMMARIES:
                cand = full / name
                if cand.is_symlink() or cand.exists():
                    real = self.contain(cand)  # a symlinked child must not lead outside the root
                    if real.is_file() and self.sniff_schema(real):
                        return self.load_document(real)
            docs = sorted(
                real
                for p in full.glob("*.json")
                if (real := self.inside(p)) is not None and real.is_file() and self.sniff_schema(real)
            )
            if len(docs) != 1:
                found = ", ".join(self.rel(p) for p in docs) or "none"
                raise QtError(
                    "QT-INPUT-001",
                    f"{self.rel(full)}: expected one qt.* summary, found {found}",
                    "pass the path of one JSON file (see qt_local_list_outputs)",
                )
            return self.load_document(docs[0])
        if not full.exists():
            raise QtError("QT-INPUT-001", f"{self.rel(full)}: no such file or directory")
        return self.load_document(full)

    def list_outputs(self, path: str) -> dict[str, Any]:
        """`{entries: [{path, schema}]}`: every `qt.*` JSON document under `path` (depth <= 6, sorted)."""
        full = self.resolve(path)
        if not full.exists():
            raise QtError("QT-INPUT-001", f"{self.rel(full)}: no such file or directory")
        if full.is_file():
            schema = self.sniff_schema(full)
            return {"entries": [{"path": self.rel(full), "schema": schema}] if schema else []}
        entries: list[dict[str, str]] = []
        scanned = 0
        base_depth = len(full.parts)
        for dirpath, dirnames, filenames in os.walk(full, followlinks=False):
            here = Path(dirpath)
            dirnames[:] = sorted(
                d for d in dirnames if not d.startswith(".") and len(here.parts) - base_depth < MAX_DEPTH
            )
            for name in sorted(filenames):
                if not name.endswith(".json") or name.startswith("."):
                    continue
                scanned += 1
                if scanned > MAX_SCANNED_FILES:
                    break
                real = self.inside(here / name)
                if real is None or not real.is_file():  # symlinks leading outside the root are skipped
                    continue
                schema = self.sniff_schema(real)
                if schema:
                    entries.append({"path": self.rel(here / name), "schema": schema})
        entries.sort(key=lambda e: e["path"])
        return {"entries": entries[:MAX_ENTRIES]}

    def check_tree(self, top: Path) -> None:
        """Raises `QT-CONFIG-001` when any entry under `top` is a symlink that resolves outside the root (the engine
        reads every file of a dataset directory); at most `MAX_DATASET_ENTRIES` entries are walked."""
        check_dataset_tree(top, self.inside, self.rel(top), require_manifest=False)

    def why(self, dataset_path: str, shadows_path: str, shadow_id: str, run: Callable[[str], str]) -> dict[str, Any]:
        """Runs the why-trace for one shadow (`run` = `_core.why`) with `out = null`; returns `qt.why_trace`."""
        dataset = self.resolve(dataset_path)
        shadows = self.resolve(shadows_path)
        if not dataset.is_dir():
            raise QtError("QT-INPUT-001", f"{self.rel(dataset)}: dataset directory not found")
        if not shadows.is_file():
            raise QtError("QT-INPUT-001", f"{self.rel(shadows)}: shadows file not found")
        self.check_tree(dataset)
        spec = sp.WhySpec(
            dataset=sp.PathRef(path=str(dataset)), shadows=sp.PathRef(path=str(shadows)), shadow_id=shadow_id, out=None
        )
        text = run(spec.model_dump_json())
        try:
            doc = json.loads(text, parse_constant=_reject_constant)
        except ValueError as e:
            raise QtError("QT-ENGINE-001", f"why returned invalid JSON: {e}") from e
        if not isinstance(doc, dict) or doc.get("schema") != "qt.why_trace":
            raise QtError("QT-ENGINE-001", "why did not return a qt.why_trace document")
        try:
            parse_document(doc)
        except ValidationError as e:
            raise QtError("QT-ENGINE-001", f"why returned an invalid qt.why_trace: {e.errors()[0]['msg']}") from e
        return doc


def core_why(spec_json: str) -> str:
    """`queuetruth._core.why` with `QtCoreError` mapped to `QtError`."""
    try:
        from queuetruth import _core
    except ImportError as e:
        raise QtError("QT-ENGINE-001", "queuetruth._core is not available", "run `make develop`") from e
    try:
        return _core.why(spec_json)
    except _core.QtCoreError as e:
        if len(e.args) >= 2:
            raise QtError(str(e.args[0]), str(e.args[1])) from e
        raise QtError.from_display(str(e)) from e
    except NotImplementedError as e:
        raise QtError("QT-ENGINE-001", f"why is not implemented in this build: {e}") from e


# ---- run mode: sandboxed local runs -------------------------------------------------------------------------------

ENV_ALLOW_RUN = "QT_MCP_ALLOW_RUN"
ENV_RUN_ROOT = "QT_MCP_RUN_ROOT"
ENV_DATA_ROOTS = "QT_MCP_DATA_ROOTS"
ENV_ALLOW_NETWORK = "QT_MCP_ALLOW_NETWORK"
DEFAULT_RUN_DIR = "qt-mcp-runs"
STAGING_DIR = ".staging"
#: The child target of every run: `queuetruth.runner.run` on the operation named in the request.
OP_TARGET = "queuetruth.mcp.local:_child_op"


def run_operations() -> dict[str, OperationDef]:
    """The operations run mode runs: every operation of the table with an MCP tool, by name."""
    return {name: op for name, op in table().items() if op.tool is not None}


def _schema_name(op: OperationDef) -> str | None:
    return getattr(op.document, "SCHEMA", None)


#: Run kinds (operation names) -> (spec model, child target, summary schema or None for a verb's document).
RUN_KINDS: dict[str, tuple[type[Any], str, str | None]] = {
    name: (op.spec, OP_TARGET, _schema_name(op)) for name, op in run_operations().items()
}
WHY_TARGET = "queuetruth.mcp.local:_child_why"
#: Environment variables a run child inherits (everything else, `QT_*` included, is dropped).
CHILD_ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT")
MAX_LINKS = 100
#: Poll interval of the run watchdog (memory, output bytes, free disk).
WATCH_POLL_S = 0.25
#: Staging entries older than the run timeout plus this margin are left over from a killed server and are removed.
STALE_STAGING_MARGIN_S = 300.0
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")
_BOOT = (
    "import json, sys\n"
    "req = json.loads(sys.stdin.buffer.read())\n"
    "sys.path[:0] = [p for p in req['sys_path'] if p not in sys.path]\n"
    "from queuetruth.mcp.local import _child_main\n"
    "_child_main(req)\n"
)


def env_flag(name: str, env: Mapping[str, str] | None = None, default: bool = False) -> bool:
    """A boolean environment setting (`1/true/yes/on` or `0/false/no/off`; unset or empty = `default`); anything
    else is QT-CONFIG-001."""
    value = (os.environ if env is None else env).get(name, "").strip().lower()
    if value == "":
        return default
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise QtError("QT-CONFIG-001", f"{name}={value!r}: expected 1/true/yes/on or 0/false/no/off")


@dataclass(frozen=True)
class RunLimits:
    """Resource limits of local runs; `from_env` reads `QT_MCP_RUN_<FIELD>` (upper case) for each field."""

    #: Wall-clock limit of one run (seconds).
    timeout_s: float = 900.0
    #: CPU time of one run, all threads (`RLIMIT_CPU`, seconds).
    cpu_s: int = 3600
    #: Memory of one run (footprint/RSS watchdog; bytes, 0 = no limit).
    memory_bytes: int = 8 << 30
    #: Output bytes of one run: each file (`RLIMIT_FSIZE`) and the total (polled while it runs, checked after).
    max_output_bytes: int = 8 << 30
    #: Free disk the run root must keep: checked before a run and polled while it runs.
    min_free_bytes: int = 2 << 30
    #: Largest `threads` a spec may ask for.
    max_threads: int = max(1, min(4, os.cpu_count() or 1))
    #: Most shadows one run may read or sample.
    max_shadows: int = 200_000
    #: Largest `particles`, `bootstrap_b`, zoo model list and horizon list.
    max_particles: int = 10_000
    max_bootstrap: int = 10_000
    max_models: int = 64
    max_horizons: int = 32
    #: Runs at once (further calls wait).
    max_concurrent: int = 1

    def __post_init__(self) -> None:
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name == "timeout_s":
                ok = isinstance(v, int | float) and not isinstance(v, bool) and v > 0 and v != float("inf")
                need = "a number > 0"
            else:
                floor = 0 if f.name in ("memory_bytes", "min_free_bytes") else 1
                ok = isinstance(v, int) and not isinstance(v, bool) and v >= floor
                need = f"an integer >= {floor}"
            if not ok:
                raise QtError("QT-CONFIG-001", f"run limit {f.name} must be {need}, got {v!r}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RunLimits:
        """Defaults overridden by `QT_MCP_RUN_TIMEOUT_S`, `QT_MCP_RUN_MAX_THREADS`, ... (bad values: QT-CONFIG-001)."""
        src = os.environ if env is None else env
        kw: dict[str, Any] = {}
        for f in fields(cls):
            name = f"QT_MCP_RUN_{f.name.upper()}"
            raw = src.get(name)
            if raw is None or raw.strip() == "":
                continue
            try:
                kw[f.name] = float(raw) if f.type in ("float", float) else int(raw.strip())
            except ValueError:
                raise QtError("QT-CONFIG-001", f"{name}={raw!r}: not a number") from None
        return cls(**kw)


def _split_roots(value: str | None) -> list[str]:
    return [p for p in (value or "").split(os.pathsep) if p.strip()]


def _too_broad(path: Path) -> bool:
    """`path` (a realpath) is a filesystem anchor, the home directory or one of its parents."""
    if path == Path(path.anchor):
        return True
    try:
        home = Path(os.path.realpath(Path.home()))
    except (RuntimeError, KeyError, OSError):
        return False
    return path == home or path in home.parents


def run_mode_unsupported() -> str | None:
    """Why run mode cannot work on this platform (no process groups or resource limits), else None."""
    if not hasattr(os, "killpg") or not hasattr(os, "setsid"):
        return "process groups (os.killpg)"
    try:
        import resource  # noqa: F401
    except ImportError:
        return "the resource module"
    return None


class Sandbox(Roots):
    """Allowed input roots and the run root of run mode (every path is a realpath): `queuetruth.runner.Roots` with
    `QT_MCP_ROOT` first (relative paths start there), then `QT_MCP_DATA_ROOTS` and the run root."""

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        data_roots: Sequence[str | os.PathLike[str]] | None = None,
        run_root: str | os.PathLike[str] | None = None,
    ) -> None:
        base = root if root is not None else os.environ.get(ENV_ROOT) or os.getcwd()
        real_base = Path(os.path.realpath(Path(base).expanduser()))
        extra = data_roots if data_roots is not None else _split_roots(os.environ.get(ENV_DATA_ROOTS))
        run = run_root if run_root is not None else os.environ.get(ENV_RUN_ROOT) or real_base / DEFAULT_RUN_DIR
        self.run_root = Path(os.path.realpath(Path(run).expanduser()))
        super().__init__([real_base, *extra, self.run_root], base=real_base)
        self.env_hint = f"{ENV_ROOT}, {ENV_DATA_ROOTS}, {ENV_RUN_ROOT}"
        for r in self.roots:
            if _too_broad(r):
                raise QtError(
                    "QT-CONFIG-001",
                    f"run mode: allowed root {r} is the filesystem root, the home directory or a parent of it",
                    f"set {ENV_ROOT} (default: the working directory), {ENV_DATA_ROOTS} and {ENV_RUN_ROOT} to the "
                    "directories that hold the datasets and the run outputs",
                )


@dataclass
class RunResult:
    """One finished run: the summary document, its output directory and whether it was reused."""

    kind: str
    summary: dict[str, Any]
    out: Path
    cached: bool
    #: the answer line of the result (`.summary` of the shared result shape), when it has one
    answer: str | None = None
    warnings: list[str] = field(default_factory=list)


def child_env(workdir: str, threads: int) -> dict[str, str]:
    """The complete environment of a run child."""
    env = {k: v for k, v in os.environ.items() if k in CHILD_ENV_ALLOW}
    env.update(
        {
            "HOME": workdir,
            "TMPDIR": workdir,
            "TMP": workdir,
            "TEMP": workdir,
            "MPLCONFIGDIR": workdir,
            "PYTHONDONTWRITEBYTECODE": "1",
            "RAYON_NUM_THREADS": str(threads),
            "OMP_NUM_THREADS": str(threads),
        }
    )
    return env


def _parquet_rows(path: Path, what: str) -> int:
    import pyarrow.parquet as pq

    try:
        return int(pq.ParquetFile(path).metadata.num_rows)
    except (OSError, ValueError) as e:
        raise QtError("QT-INPUT-002", f"{what}: not a readable Parquet file: {type(e).__name__}") from e


def _manifest_partitions(dataset: Path) -> list[tuple[int, int]]:
    """`(instrument_id, session_date)` of every partition in the dataset manifest (`QT-INPUT-002` when unreadable)."""
    try:
        manifest = read_manifest_doc(dataset, max_bytes=MAX_JSON_BYTES) or {}
        parts = manifest["partitions"]
        out = [(int(p["instrument_id"]), int(p["session_date"])) for p in parts]
    except QtError as e:
        raise QtError("QT-INPUT-002", f"spec.dataset: manifest.json has no readable partitions: {e.message}") from e
    except (ValueError, TypeError, KeyError) as e:
        raise QtError("QT-INPUT-002", f"spec.dataset: manifest.json has no readable partitions: {e}") from e
    if any(isinstance(p.get("instrument_id"), bool) or isinstance(p.get("session_date"), bool) for p in parts):
        raise QtError("QT-INPUT-002", "spec.dataset: manifest.json has no readable partitions: boolean ids")
    return out


def _date_compact(days: int) -> str:
    """`YYYYMMDD` of a UTC day count (the engine's `date_compact`)."""
    import datetime as dt

    return (dt.date(1970, 1, 1) + dt.timedelta(days=days)).strftime("%Y%m%d")


def sample_bound(dataset: Path, data: Mapping[str, Any]) -> int:
    """Shadows a `ShadowSampleSpec` can sample: `n` for `equal`/`proportional`; for `per_partition` the engine's
    quota sum over the manifest's partitions (each instrument-day takes the dated key `<id>_<YYYYMMDD>`, else the
    undated `<id>`, else `n`, so an undated key counts once per day of that instrument)."""
    n = int(data["n"])
    if data.get("allocation") != "per_partition":
        return n
    quotas = {str(k): int(v) for k, v in (data.get("partition_quotas") or {}).items()}
    total = 0
    for instrument_id, day in _manifest_partitions(dataset):
        try:
            dated = f"{instrument_id}_{_date_compact(day)}"
        except (OverflowError, ValueError):
            dated = None
        if dated is not None and dated in quotas:
            total += quotas[dated]
        else:
            total += quotas.get(str(instrument_id), n)
    return total


def _tree_bytes(top: Path) -> int:
    """Bytes of the regular files under `top` (entries that vanish while it walks are skipped)."""
    total = 0
    stack = [str(top)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _signal_name(rc: int) -> str:
    try:
        return signal.Signals(-rc).name
    except ValueError:
        return f"signal {-rc}"


class LocalRunner:
    """Runs shadow sampling, replay, zoo and why on local datasets in limited child processes (run mode)."""

    def __init__(self, sandbox: Sandbox, limits: RunLimits | None = None, *, allow_network: bool = False) -> None:
        missing = run_mode_unsupported()
        if missing is not None:
            raise QtError("QT-CONFIG-001", f"run mode needs {missing}, which this platform lacks", "use a POSIX host")
        self.sandbox = sandbox
        self.limits = limits or RunLimits()
        #: whether a run may reach the network (`QT_MCP_ALLOW_NETWORK`); off, a spec that needs it is refused
        self.allow_network = allow_network
        self._limiter: Any = None
        self._run_locks: dict[str, Any] = {}
        self.own_dir(self.sandbox.run_root / STAGING_DIR)
        self.sweep_staging()

    def sweep_staging(self, now: float | None = None) -> list[str]:
        """Removes `<run root>/.staging` entries left by a killed server: discarded directories (`trash-*`) and
        staging directories not modified for longer than the run timeout plus `STALE_STAGING_MARGIN_S` (no run
        outlives its timeout). Returns the removed names."""
        import time

        staging = self.sandbox.run_root / STAGING_DIR
        cutoff = (time.time() if now is None else now) - self.limits.timeout_s - STALE_STAGING_MARGIN_S
        removed = []
        try:
            entries = list(os.scandir(staging))
        except OSError:
            return removed
        for e in entries:
            try:
                stale = e.name.startswith("trash-") or e.stat(follow_symlinks=False).st_mtime < cutoff
            except OSError:
                continue
            if stale:
                _remove(Path(e.path))
                removed.append(e.name)
        return sorted(removed)

    # ---- validation -------------------------------------------------------------------------------------------

    def _knob(self, name: str, value: int, limit: int) -> None:
        if value > limit:
            raise QtError(
                "QT-CONFIG-001",
                f"spec.{name} = {value} exceeds this server's limit {limit}",
                f"set QT_MCP_RUN_{_LIMIT_ENV.get(name, name).upper()} to raise it",
            )

    def operation(self, kind: str) -> OperationDef:
        """The operation of a run kind (`QT-USAGE-001` for one run mode does not run)."""
        ops = run_operations()
        if kind not in ops:
            raise QtError("QT-USAGE-001", f"unknown run kind {kind!r}")
        return ops[kind]

    def prepare(self, kind: str, spec: Any) -> Any:
        """Validates a run spec: model, no output location (`out` null), path references inside the roots, compute
        knobs and shadow counts within `RunLimits`, and no network unless allowed. Returns the spec with resolved
        real paths."""
        op = self.operation(kind)
        model = op.spec
        if isinstance(spec, BaseModel):
            spec = spec.model_dump(mode="json")
        if not isinstance(spec, dict):
            raise QtError("QT-USAGE-001", "spec must be a JSON object")
        if op.out_field is not None and spec.get(op.out_field) is not None:
            raise QtError("QT-CONFIG-001", f"spec.{op.out_field} must be null: local runs write under the run root")
        try:
            parsed = model.model_validate(spec)
        except ValidationError as e:
            err = e.errors()[0]
            where = ".".join(str(x) for x in err.get("loc", ()))
            raise QtError("QT-USAGE-001", f"invalid {model.__name__}: {where or '<spec>'}: {err['msg']}") from e
        doc = parsed.model_dump(mode="json")
        resolved = model.model_validate(self.sandbox.refs(doc, "spec", resolve=True, path_refs_only=True))
        data = resolved.model_dump(mode="json")
        lim = self.limits
        dataset: Path | None = None
        if isinstance(data.get("dataset"), dict):
            dataset = Path(data["dataset"]["path"])
            if not dataset.is_dir():
                raise QtError("QT-INPUT-001", f"spec.dataset: {self.sandbox.rel(dataset)} is not a dataset directory")
        for ref in ("shadows", "amends", "parity_bbo"):
            if isinstance(data.get(ref), dict) and not Path(data[ref]["path"]).is_file():
                raise QtError("QT-INPUT-001", f"spec.{ref}: {self.sandbox.rel(Path(data[ref]['path']))} is not a file")
        if data.get("threads") is not None:
            self._knob("threads", int(data["threads"]), lim.max_threads)
        if data.get("particles") is not None:
            self._knob("particles", int(data["particles"]), lim.max_particles)
        if data.get("bootstrap_b") is not None:
            self._knob("bootstrap_b", int(data["bootstrap_b"]), lim.max_bootstrap)
        if kind == "zoo":
            self._knob("models", len(data["models"]), lim.max_models)
            self._knob("horizons_ns", len(data["horizons_ns"]), lim.max_horizons)
        if kind == "shadows":
            assert dataset is not None
            self._knob("n", sample_bound(dataset, data), lim.max_shadows)
        elif isinstance(data.get("shadows"), dict):
            self._knob("shadows", _parquet_rows(Path(data["shadows"]["path"]), "spec.shadows"), lim.max_shadows)
        for name in ("n_shadows", "n"):  # the verbs' own shadow counts (fit, demo)
            if kind != "shadows" and isinstance(data.get(name), int):
                self._knob(name, int(data[name]), lim.max_shadows)
        if op.uses_network(resolved) and not self.allow_network:
            raise QtError(
                "QT-CONFIG-001",
                f"{kind}: this spec needs the network, which this server does not allow",
                f"give a local source in the spec, or set {ENV_ALLOW_NETWORK}=1",
            )
        return resolved

    def prepare_why(self, dataset_path: str, shadows_path: str, shadow_id: str) -> dict[str, Any]:
        """The `WhySpec` of `qt_local_why` in run mode (inputs inside the allowed roots, `out` null). It traces one
        shadow, so the shadows file is not bounded by `max_shadows`; the child's limits bound the work."""
        dataset = self.sandbox.resolve(dataset_path, "dataset_path")
        shadows = self.sandbox.resolve(shadows_path, "shadows_path")
        if not dataset.is_dir():
            raise QtError("QT-INPUT-001", f"{self.sandbox.rel(dataset)}: dataset directory not found")
        if not shadows.is_file():
            raise QtError("QT-INPUT-001", f"{self.sandbox.rel(shadows)}: shadows file not found")
        self.sandbox.check_tree(dataset, "dataset_path")
        spec = sp.WhySpec(
            dataset=sp.PathRef(path=str(dataset)), shadows=sp.PathRef(path=str(shadows)), shadow_id=shadow_id, out=None
        )
        return spec.model_dump(mode="json")

    # ---- runs -------------------------------------------------------------------------------------------------

    def out_dir(self, kind: str, run_id: str) -> Path:
        """`<run root>/<kind>/<run_id>`."""
        return self.sandbox.run_root / kind / run_id

    def own_dir(self, path: Path) -> Path:
        """Creates `path`, a directory under the run root, and checks that no part of it below the run root is a
        symlink (so a run never writes through a link out of the run root)."""
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise QtError(
                "QT-CONFIG-001",
                f"cannot create the run directory {path}: {e.strerror or e}",
                f"set {ENV_RUN_ROOT} to a writable directory",
            ) from None
        if Path(os.path.realpath(path)) != path:
            raise QtError("QT-CONFIG-001", f"{path} is or lies under a symlink; run outputs stay inside the run root")
        return path

    def cached(self, kind: str, run_id: str) -> dict[str, Any] | None:
        """The summary of a finished run directory for this `run_id`, if one exists and validates."""
        out = self.out_dir(kind, run_id)
        path = out / "summary.json"
        if not path.is_file() or path.is_symlink() or Path(os.path.realpath(out)) != out:
            return None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
            if not isinstance(doc, dict) or doc.get("schema") != _schema_name(self.operation(kind)):
                return None
            parse_document(doc)
        except (OSError, ValueError, ValidationError):
            return None
        if "run_id" in doc and doc["run_id"] != run_id:
            return None
        return doc

    async def _slot(self) -> Any:
        import anyio

        if self._limiter is None:
            self._limiter = anyio.CapacityLimiter(self.limits.max_concurrent)
        return self._limiter

    def _check_disk(self) -> int:
        """Free bytes under the run root (`QT-CONFIG-001` below `min_free_bytes`)."""
        self.own_dir(self.sandbox.run_root)
        free = shutil.disk_usage(self.sandbox.run_root).free
        if free < self.limits.min_free_bytes:
            raise QtError(
                "QT-CONFIG-001",
                f"only {free} bytes free under the run root; a run needs {self.limits.min_free_bytes}",
                "free disk space or lower QT_MCP_RUN_MIN_FREE_BYTES",
            )
        return free

    def _discard(self, dest: Path) -> None:
        """Moves an invalid finished directory (or a stray file or link) out of the way and removes it."""
        trash = self.sandbox.run_root / STAGING_DIR / f"trash-{dest.name}-{secrets.token_hex(4)}"
        try:
            dest.rename(trash)
        except FileNotFoundError:
            return
        except OSError as e:
            raise QtError(
                "QT-CONFIG-001",
                f"{dest} holds no valid run and cannot be moved away: {e.strerror or e}",
                "remove the directory",
            ) from None
        _remove(trash)

    async def run(self, kind: str, spec: Any) -> RunResult:
        """Validates, then reuses a finished run of the same `run_id` or runs the spec in a limited child. An
        operation without a `run_id` (the verbs) runs every time, into a new directory."""
        import anyio
        import anyio.to_thread

        from queuetruth import api

        resolved = await anyio.to_thread.run_sync(self.prepare, kind, spec)
        if not self.operation(kind).run_id:
            import time

            run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(4)}"
            async with await self._slot():
                return await self._run_new(kind, run_id, resolved, self.out_dir(kind, run_id))
        try:
            run_id = await anyio.to_thread.run_sync(api.run_id, resolved)
        except ImportError as e:
            raise QtError("QT-ENGINE-001", "queuetruth._core is not available", "run `make develop`") from e
        dest = self.out_dir(kind, run_id)
        key = f"{kind}/{run_id}"
        lock = self._run_locks.setdefault(key, anyio.Lock())
        try:
            async with lock:  # an identical spec waits for the first and then reuses its directory
                hit = self.cached(kind, run_id)
                if hit is not None:
                    return RunResult(kind, hit, dest, True)
                async with await self._slot():
                    return await self._run_new(kind, run_id, resolved, dest)
        finally:
            if not lock.locked() and lock.statistics().tasks_waiting == 0:
                self._run_locks.pop(key, None)

    async def _run_new(self, kind: str, run_id: str, resolved: Any, dest: Path) -> RunResult:
        free = self._check_disk()
        staging_root = self.own_dir(self.sandbox.run_root / STAGING_DIR)
        self.own_dir(dest.parent)
        if dest.exists() or dest.is_symlink():  # a directory whose summary does not validate (cached() was None)
            self._discard(dest)
        staging = staging_root / f"{kind}-{run_id}-{secrets.token_hex(4)}"
        staging.mkdir()
        try:
            op = self.operation(kind)
            data = resolved.model_dump(mode="json")
            if op.out_field is not None:
                data[op.out_field] = str(staging / op.out_name if op.out_name else staging)
            fsize = max(1, min(self.limits.max_output_bytes, free - self.limits.min_free_bytes))
            req = {
                "op": kind,
                "spec": data,
                "roots": [str(r) for r in self.sandbox.roots],
                "allow_network": self.allow_network,
            }
            threads = int(data.get("threads") or 1)
            got = await self.spawn(OP_TARGET, req, threads=threads, watch_dir=staging, fsize_bytes=fsize)
            if not isinstance(got, dict) or not isinstance(got.get("document"), dict):
                raise QtError("QT-ENGINE-001", f"{kind} did not return a document")
            summary = got["document"]
            if op.out_name or not (staging / "summary.json").exists():
                (staging / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            total = _tree_bytes(staging)
            if total > self.limits.max_output_bytes:
                raise QtError(
                    "QT-ENGINE-001",
                    f"{kind} outputs are {total} bytes, above the limit {self.limits.max_output_bytes}",
                    "raise QT_MCP_RUN_MAX_OUTPUT_BYTES or narrow the spec",
                )
            try:
                staging.rename(dest)
            except OSError:
                hit = self.cached(kind, run_id)  # another server placed a finished directory of this run_id
                if hit is None:
                    raise QtError("QT-ENGINE-001", f"cannot place the outputs at {dest}") from None
                return RunResult(kind, hit, dest, True)
            warns = [str(w) for w in got.get("warnings") or []]
            return RunResult(kind, summary, dest, False, got.get("summary"), warns)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    async def why(self, dataset_path: str, shadows_path: str, shadow_id: str) -> dict[str, Any]:
        """`qt.why_trace` of one shadow, computed in a limited child (`out` null: nothing is written)."""
        import anyio.to_thread

        spec = await anyio.to_thread.run_sync(self.prepare_why, dataset_path, shadows_path, shadow_id)
        async with await self._slot():
            doc = await self.spawn(WHY_TARGET, spec, threads=1)
        if not isinstance(doc, dict) or doc.get("schema") != "qt.why_trace":
            raise QtError("QT-ENGINE-001", "why did not return a qt.why_trace document")
        try:
            parse_document(doc)
        except ValidationError as e:
            raise QtError("QT-ENGINE-001", f"why returned an invalid qt.why_trace: {e.errors()[0]['msg']}") from e
        return doc

    async def spawn(
        self,
        target: str,
        spec: dict[str, Any],
        *,
        threads: int = 1,
        watch_dir: Path | None = None,
        fsize_bytes: int | None = None,
    ) -> Any:
        """Runs `target(spec)` (`"module:function"`) in a fresh `python -I` session under the limits and returns its
        result; the child's process group is killed on timeout, on a limit and when the caller is cancelled. With
        `watch_dir`, the watchdog also kills it when the bytes under `watch_dir` exceed `max_output_bytes` or the
        free disk there falls below `min_free_bytes`; `fsize_bytes` (default `max_output_bytes`) is `RLIMIT_FSIZE`."""
        import subprocess

        import anyio

        lim = self.limits
        work = tempfile.mkdtemp(prefix="qt-mcp-run-")
        result = Path(work) / "result.json"
        req = {
            "target": target,
            "spec": spec,
            "result": str(result),
            "sys_path": [p for p in sys.path if p and os.path.isabs(p)],
            "cpu_s": int(lim.cpu_s),
            "fsize_bytes": int(lim.max_output_bytes if fsize_bytes is None else fsize_bytes),
        }
        reason: str | None = None
        try:
            with open(Path(work) / "child.log", "wb") as log:
                proc = await anyio.open_process(
                    [sys.executable, "-I", "-c", _BOOT],
                    stdin=subprocess.PIPE,
                    stdout=log,
                    stderr=log,
                    cwd=work,
                    env=child_env(work, max(1, threads)),
                    start_new_session=True,
                )
            try:
                assert proc.stdin is not None
                await proc.stdin.send(json.dumps(req).encode())
                await proc.stdin.aclose()
                with anyio.move_on_after(lim.timeout_s) as scope:
                    async with anyio.create_task_group() as tg:
                        if lim.memory_bytes or watch_dir is not None:

                            async def watch() -> None:
                                nonlocal reason
                                while reason is None:
                                    await anyio.sleep(WATCH_POLL_S)
                                    reason = await self._over_limit(proc.pid, watch_dir)
                                _kill_group(proc.pid)

                            tg.start_soon(watch)
                        await proc.wait()
                        tg.cancel_scope.cancel()
                if scope.cancelled_caught:
                    reason = f"timed out after {lim.timeout_s:g} s"
            finally:
                if proc.returncode is None:
                    _kill_group(proc.pid)
                    with anyio.CancelScope(shield=True):
                        await proc.wait()
                else:
                    _kill_group(proc.pid)  # stray grandchildren of the session
            return _read_result(result, proc.returncode, reason, Path(work) / "child.log", lim)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def _over_limit(self, pid: int, watch_dir: Path | None) -> str | None:
        """The watchdog's verdict on a running child: the limit it broke, else None."""
        import anyio.to_thread

        lim = self.limits
        if lim.memory_bytes:
            used = await _memory_bytes(pid)
            if used is not None and used > lim.memory_bytes:
                return f"memory limit exceeded ({used} > {lim.memory_bytes} bytes)"
        if watch_dir is not None:
            written = await anyio.to_thread.run_sync(_tree_bytes, watch_dir)
            if written > lim.max_output_bytes:
                return f"output limit exceeded ({written} > {lim.max_output_bytes} bytes written)"
            try:
                free = shutil.disk_usage(watch_dir).free
            except OSError:
                free = None
            if free is not None and free < lim.min_free_bytes:
                return f"free disk under the run root fell to {free} bytes (floor {lim.min_free_bytes})"
        return None


_LIMIT_ENV = {
    "threads": "max_threads",
    "particles": "max_particles",
    "bootstrap_b": "max_bootstrap",
    "models": "max_models",
    "horizons_ns": "max_horizons",
    "n": "max_shadows",
    "shadows": "max_shadows",
}


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _remove(path: Path) -> None:
    """Removes a file, link or directory tree (best effort; never follows a link)."""
    try:
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def proc_status_rss(text: str) -> int | None:
    """Resident bytes from the text of `/proc/<pid>/status` (`VmRSS: <n> kB`), else None."""
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    return None


def _footprint_darwin(pid: int) -> int | None:
    """`ri_phys_footprint` of `proc_pid_rusage(pid, RUSAGE_INFO_V0)` (macOS): resident plus compressed memory, the
    figure Activity Monitor shows (plain RSS misses pages the memory compressor took)."""
    import ctypes
    import ctypes.util

    global _LIBPROC
    if _LIBPROC is None:
        _LIBPROC = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib", use_errno=True)
    buf = ctypes.create_string_buffer(256)  # rusage_info_v0 is 96 bytes
    if _LIBPROC.proc_pid_rusage(ctypes.c_int(pid), ctypes.c_int(0), buf) != 0:
        return None
    return int.from_bytes(buf.raw[72:80], sys.byteorder)  # 16-byte uuid, then 7 uint64 before phys_footprint


_LIBPROC: Any = None


async def _memory_bytes(pid: int) -> int | None:
    """Memory of `pid` for the watchdog: the physical footprint on macOS, `VmRSS` from `/proc` on Linux, else RSS
    from `ps` (None when unreadable, e.g. the process exited)."""
    if sys.platform == "darwin":
        try:
            return _footprint_darwin(pid)
        except (OSError, AttributeError):
            pass
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as f:
                return proc_status_rss(f.read())
        except OSError:
            return None
    return await _ps_rss(pid)


async def _ps_rss(pid: int) -> int | None:
    """RSS of `pid` from `ps -o rss=` (kB), else None."""
    import anyio

    try:
        out = await anyio.run_process(["ps", "-o", "rss=", "-p", str(pid)], check=False)
    except OSError:
        return None
    text = out.stdout.decode().strip()
    return int(text) * 1024 if text.isdigit() else None


def _tail(path: Path, n: int = 400) -> str:
    try:
        data = path.read_bytes()[-n:]
    except OSError:
        return ""
    return data.decode("utf-8", "replace").strip().splitlines()[-1] if data.strip() else ""


def _read_result(result: Path, rc: int | None, reason: str | None, log: Path, lim: RunLimits) -> Any:
    if reason is not None:
        raise QtError("QT-ENGINE-001", f"local run {reason}", "raise the QT_MCP_RUN_* limit or narrow the spec")
    msg: Any = None
    if rc == 0 and result.is_file():
        try:
            msg = json.loads(result.read_text(encoding="utf-8"))
        except ValueError:
            msg = None
    if isinstance(msg, dict) and msg.get("status") == "ok":
        return msg.get("result")
    if isinstance(msg, dict) and msg.get("status") == "error":
        err = msg.get("error") or {}
        raise QtError(str(err.get("code", "QT-ENGINE-001")), str(err.get("message", "run failed")), err.get("hint"))
    if rc is not None and rc < 0:
        name = _signal_name(rc)
        if name == "SIGXCPU":
            raise QtError(
                "QT-ENGINE-001", f"local run exceeded its CPU limit ({lim.cpu_s} s)", "raise QT_MCP_RUN_CPU_S"
            )
        if name == "SIGXFSZ":
            raise QtError(
                "QT-ENGINE-001",
                f"local run wrote a file above the output limit ({lim.max_output_bytes} bytes)",
                "raise QT_MCP_RUN_MAX_OUTPUT_BYTES",
            )
        raise QtError("QT-ENGINE-001", f"local run was killed by {name}")
    tail = _tail(log)
    raise QtError("QT-ENGINE-001", f"local run exited with code {rc}" + (f": {tail}" if tail else ""))


# ---- child side ---------------------------------------------------------------------------------------------------


def _no_unpickling(event: str, args: tuple[Any, ...]) -> None:
    if event == "pickle.find_class":
        raise RuntimeError("unpickling is disabled in local runs")


def _set_limits(req: dict[str, Any]) -> None:
    import resource

    for which, value in (
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_CPU, int(req.get("cpu_s") or 0)),
        (resource.RLIMIT_FSIZE, int(req.get("fsize_bytes") or 0)),
    ):
        if value == 0 and which != resource.RLIMIT_CORE:
            continue
        try:
            _, hard = resource.getrlimit(which)
            soft = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(which, (soft, hard))
        except (ValueError, OSError):
            pass


def _error_doc(e: BaseException) -> dict[str, Any]:
    if isinstance(e, QtError):
        return {"code": e.code, "message": e.message, "hint": e.hint}
    code, message = getattr(e, "code", None), getattr(e, "message", None)
    if isinstance(code, str) and code.startswith("QT-") and isinstance(message, str):
        return {"code": code, "message": message, "hint": None}
    if isinstance(e, MemoryError):
        return {"code": "QT-ENGINE-001", "message": "out of memory", "hint": "raise QT_MCP_RUN_MEMORY_BYTES"}
    return {"code": "QT-ENGINE-001", "message": f"internal error: {type(e).__name__}: {str(e)[:500]}", "hint": None}


def _child_main(req: dict[str, Any]) -> None:
    """Entry point of a run child (`python -I -c _BOOT`): limits, guards, target, result file, `os._exit`."""
    import importlib

    out: dict[str, Any]
    try:
        _set_limits(req)
        sys.addaudithook(_no_unpickling)
        module, _, name = str(req["target"]).partition(":")
        fn = getattr(importlib.import_module(module), name)
        out = {"status": "ok", "result": fn(req["spec"])}
    except BaseException as e:
        out = {"status": "error", "error": _error_doc(e)}
    try:
        data = json.dumps(out, default=str)
    except (TypeError, ValueError) as e:
        data = json.dumps({"status": "error", "error": _error_doc(e)})
    try:
        with open(req["result"], "w", encoding="utf-8") as f:
            f.write(data)
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


def _dump_doc(doc: Any) -> dict[str, Any]:
    return doc.model_dump(mode="json", by_alias=True)


def _child_op(req: dict[str, Any]) -> dict[str, Any]:
    """A run child: `queuetruth.runner.run` on the operation of the request, with the sandbox's roots (the spec's
    paths were resolved in the parent; the runner checks them again) and its network setting."""
    from queuetruth.runner import RunContext, run

    ctx = RunContext(roots=req["roots"], path_refs_only=True, allow_network=bool(req.get("allow_network")))
    res = run(str(req["op"]), req["spec"], context=ctx)
    return {"document": res.document, "summary": res.summary, "warnings": res.warnings}


def _child_why(spec: dict[str, Any]) -> dict[str, Any]:
    doc = json.loads(core_why(json.dumps(spec)), parse_constant=_reject_constant)
    if not isinstance(doc, dict):
        raise QtError("QT-ENGINE-001", "why returned invalid JSON")
    return doc


def output_links(out: Path, limit: int = MAX_LINKS) -> tuple[list[tuple[str, Path, int]], int]:
    """`(relative name, path, bytes)` of the files of a finished run (sorted, at most `limit`) and the total count."""
    files = sorted(p for p in out.rglob("*") if p.is_file() and not p.is_symlink())
    return [(p.relative_to(out).as_posix(), p, p.stat().st_size) for p in files[:limit]], len(files)


__all__ = [
    "ENV_ALLOW_NETWORK",
    "ENV_ALLOW_RUN",
    "ENV_DATA_ROOTS",
    "ENV_ROOT",
    "ENV_RUN_ROOT",
    "OP_TARGET",
    "RUN_KINDS",
    "LocalOutputs",
    "LocalRunner",
    "RunLimits",
    "RunResult",
    "Sandbox",
    "check_dataset_tree",
    "core_why",
    "env_flag",
    "output_links",
    "proc_status_rss",
    "run_mode_unsupported",
    "run_operations",
    "sample_bound",
]
