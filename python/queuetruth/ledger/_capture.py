"""What gets recorded and where: the on/off switch, the ledger location, the record of a run (from its spec echo
and summary) or of a user-registered variant, the `recorded` decorator of `queuetruth.api` and the CLI hook for
commands the Rust core runs natively.

Recording is a side output. It runs after the entry point returned (or raised), reads the spec and the summary, and
hashes the data references the spec names; it never touches the spec, the outputs or the result, and a failure to
record is logged (`queuetruth.ledger` logger, level warning) and never raised from the entry point.
"""

from __future__ import annotations

import datetime
import functools
import hashlib
import json
import logging
import math
import os
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from queuetruth.errors import QtError
from queuetruth.ledger import _series
from queuetruth.ledger._store import GENESIS_PREV, Draft, Ledger, now_ns
from queuetruth.ledger.models import LABEL, RUN_KINDS, CodeVersion, ErrorInfo, LedgerRecord, Series
from queuetruth.schemas.runid import EXECUTION_ONLY, canonical_json, drop_v1_1_defaults, is_ref
from queuetruth.schemas.runid import run_id as _run_id

log = logging.getLogger("queuetruth.ledger")

ENV_SWITCH = "QT_LEDGER"
ENV_DIR = "QT_LEDGER_DIR"
ENV_STUDY = "QT_LEDGER_STUDY"
DEFAULT_STUDY = "default"
#: Values of `QT_LEDGER` that turn recording off / force it on (case-insensitive).
OFF_VALUES = frozenset({"0", "off", "false", "no", "disabled"})
ON_VALUES = frozenset({"1", "on", "true", "yes", "enabled"})
#: Files whose presence marks a project root for the default location.
PROJECT_MARKERS = (".queuetruth", "pyproject.toml", ".git")
_LABEL = re.compile(LABEL)
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_GIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
MAX_METRIC_TEXT = 512

F = TypeVar("F", bound=Callable[..., Any])

#: Keys a summary's spec echo adds that are not spec fields (left out of `spec_hash` and `run_id`).
ECHO_ONLY: dict[str, tuple[str, ...]] = {"calibrate-live": ("calibration_source",)}

#: Set while a recorded entry point runs, so nested entry points (score -> zoo) are one trial, not two.
_ACTIVE: ContextVar[bool] = ContextVar("queuetruth_ledger_active", default=False)
_STUDY: ContextVar[str | None] = ContextVar("queuetruth_ledger_study", default=None)
_FILE_HASHES: dict[tuple[str, int, int], str] = {}


# ---- switch and location ------------------------------------------------------------------------------------------


def switch() -> str:
    """`off`, `on` or `auto` from `QT_LEDGER` (unset or another value: `auto`)."""
    raw = os.environ.get(ENV_SWITCH, "").strip().lower()
    if raw in OFF_VALUES:
        return "off"
    if raw in ON_VALUES:
        return "on"
    return "auto"


def auto_enabled() -> bool:
    """True when entry points record their runs automatically. Never in the HTTP service (a process that imported
    `queuetruth_service`): one local file would mix tenants (SPEC 8.8). Off with `QT_LEDGER=off`. Under pytest
    (`PYTEST_CURRENT_TEST` set) off unless `QT_LEDGER=on` or `QT_LEDGER_DIR` is set, so that test suites do not add
    synthetic runs to a research ledger."""
    if "queuetruth_service" in sys.modules:
        return False
    sw = switch()
    if sw != "auto":
        return sw == "on"
    return not (os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get(ENV_DIR, "").strip())


def project_root(start: Path | None = None) -> Path | None:
    """The nearest directory from `start` (default: the working directory) upwards holding `.queuetruth/`,
    `pyproject.toml` or `.git`; None when none does."""
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if any((d / m).exists() for m in PROJECT_MARKERS):
            return d
    return None


def user_data_dir() -> Path:
    """queuetruth's per-user data directory: `~/Library/Application Support/queuetruth` (macOS),
    `%LOCALAPPDATA%\\queuetruth` (Windows), else `$XDG_DATA_HOME/queuetruth` (default `~/.local/share`)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "queuetruth"
    if os.name == "nt":  # pragma: no cover - Windows
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "queuetruth"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "queuetruth"


def user_ledger() -> Ledger:
    """The per-user ledger, `<user_data_dir()>/ledger`."""
    return Ledger(user_data_dir() / "ledger")


def default_ledger() -> Ledger:
    """`$QT_LEDGER_DIR`, else `<project root>/.queuetruth/ledger` (created with a `.gitignore` that keeps the
    ledger out of git; delete that file to commit the ledger), else, outside any project, the per-user ledger
    (`user_ledger()`). Never the bare working directory: a ledger there could land inside a run's own input."""
    env = os.environ.get(ENV_DIR, "").strip()
    if env:
        return Ledger(env)
    root = project_root()
    if root is None:
        return user_ledger()
    return Ledger(root / ".queuetruth" / "ledger", ignore_in_git=True)


def _input_dirs(spec: Any) -> list[Path]:
    out = []
    for _, ref in _refs(spec):
        p = ref.get("path")
        if isinstance(p, str):
            try:
                d = Path(p).resolve()
            except (OSError, RuntimeError):
                continue
            if d.is_dir():
                out.append(d)
    return out


def _inside(led: Ledger, dirs: list[Path]) -> Path | None:
    where = led.directory.resolve()
    return next((d for d in dirs if where == d or where.is_relative_to(d)), None)


def ledger_for(spec: Any) -> Ledger | None:
    """The ledger a run of `spec` is recorded in: `default_ledger()`, unless that lies inside a directory the spec
    reads (a live log or dataset without a manifest is identified by every file under it, so a ledger there would
    change the next run's `run_id`); then the per-user ledger, with a warning; None (not recorded, with a warning)
    when that lies inside an input too."""
    led = default_ledger()
    dirs = _input_dirs(spec)
    hit = _inside(led, dirs)
    if hit is None:
        return led
    alt = user_ledger()
    if _inside(alt, dirs) is None:
        log.warning(
            "ledger: %s is inside the input directory %s, which the run is identified by; recorded in %s instead "
            "(set QT_LEDGER_DIR outside your inputs)",
            led.directory,
            hit,
            alt.directory,
        )
        return alt
    log.warning("ledger: every ledger location is inside the input directory %s; the run is not recorded", hit)
    return None


def as_ledger(ledger: Ledger | str | os.PathLike[str] | None) -> Ledger:
    """`ledger` as a `Ledger` (a path is a directory or `.jsonl` file); None = `default_ledger()`."""
    if ledger is None:
        return default_ledger()
    return ledger if isinstance(ledger, Ledger) else Ledger(ledger)


def current_study(study: str | None = None) -> str:
    """`study`, else the innermost `with study(...)`, else `$QT_LEDGER_STUDY`, else `default`."""
    s = study or _STUDY.get() or os.environ.get(ENV_STUDY, "").strip() or DEFAULT_STUDY
    if not _LABEL.match(s):
        raise QtError("QT-CONFIG-001", f"study: {s!r} must match {LABEL} (letters, digits, _ . : -; 1 to 128)")
    return s


@contextmanager
def study(name: str) -> Iterator[str]:
    """Records the runs and variants of the block under study `name` (a study is the set of trials searched for
    one claim; selection statistics count the trials of one study)."""
    token = _STUDY.set(current_study(name))
    try:
        yield name
    finally:
        _STUDY.reset(token)


# ---- code version -------------------------------------------------------------------------------------------------


@functools.cache
def _core_version() -> str | None:
    try:
        from queuetruth import _core

        return str(_core.version())
    except Exception:  # no compiled extension: recorded as null
        return None


def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def git_head(root: Path | None = None) -> str | None:
    """HEAD commit of the queuetruth source checkout the package was imported from (git worktrees and packed refs
    included); None for an installed wheel or anything unreadable."""
    if root is None:
        root = Path(__file__).resolve().parents[3]
        if not ((root / "crates").is_dir() and (root / "python" / "queuetruth").is_dir()):
            return None
    dotgit = root / ".git"
    if dotgit.is_file():
        text = _read(dotgit) or ""
        if not text.startswith("gitdir:"):
            return None
        gitdir = (root / text.removeprefix("gitdir:").strip()).resolve()
    elif dotgit.is_dir():
        gitdir = dotgit
    else:
        return None
    head = _read(gitdir / "HEAD")
    if head is None:
        return None
    if not head.startswith("ref: "):
        return head if _GIT_SHA.match(head) else None
    ref = head.removeprefix("ref: ").strip()
    common = gitdir
    rel = _read(gitdir / "commondir")
    if rel:
        common = (gitdir / rel).resolve()
    for base in (gitdir, common):
        sha = _read(base / ref)
        if sha and _GIT_SHA.match(sha):
            return sha
    packed = _read(common / "packed-refs") or ""
    for line in packed.splitlines():
        sha, _, name = line.partition(" ")
        if name.strip() == ref and _GIT_SHA.match(sha):
            return sha
    return None


def project_git(start: Path | None = None) -> str | None:
    """HEAD commit of the git checkout holding `start` (default: the working directory); None outside one."""
    try:
        here = (start or Path.cwd()).resolve()
    except OSError:
        return None
    for d in (here, *here.parents):
        if (d / ".git").exists():
            return git_head(d)
    return None


def code_version(user: str | None = None) -> CodeVersion:
    """The running code: package, engine and core versions, the queuetruth source checkout's commit, the commit
    of the caller's own checkout (the working directory's), and `user`."""
    from queuetruth._version import __version__
    from queuetruth.schemas.arrow import ENGINE_VERSION

    return CodeVersion(
        queuetruth=__version__,
        engine=ENGINE_VERSION,
        core=_core_version(),
        git=git_head(),
        project_git=project_git(),
        user=user,
    )


# ---- identities ---------------------------------------------------------------------------------------------------


def _h(doc: Any) -> str:
    return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()


def _config(node: Any) -> Any:
    if isinstance(node, dict):
        if is_ref(node):
            return "$ref"
        return {k: _config(v) for k, v in node.items() if k not in EXECUTION_ONLY}
    if isinstance(node, list):
        return [_config(v) for v in node]
    return node


def spec_hash(kind: str, spec: dict[str, Any]) -> str:
    """First 16 hex of sha256 of `{"kind", "spec"}` with execution-only fields and defaulted 1.1 fields removed
    and every data reference replaced by `"$ref"`: the configuration, independent of data and engine version."""
    return _h({"kind": kind, "spec": _config(drop_v1_1_defaults(spec))})[:16]


def _refs(node: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(node, dict):
        if is_ref(node):
            yield ".".join(path), node
            return
        for k in sorted(node):
            if k not in EXECUTION_ONLY:
                yield from _refs(node[k], (*path, k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _refs(v, (*path, str(i)))


def _file_sha(p: str) -> str | None:
    """sha256 of the regular file at `p`, cached by (path, size, mtime) for the process; None when `p` is not one."""
    from queuetruth.schemas.io import sha256_file

    try:
        path = Path(p).resolve()
        st = path.stat()
    except (OSError, RuntimeError):
        return None
    if not path.is_file():
        return None
    key = (str(path), st.st_size, st.st_mtime_ns)
    got = _FILE_HASHES.get(key)
    if got is None:
        got = _FILE_HASHES[key] = sha256_file(path)
    return got


def resolve(ref: dict[str, Any]) -> str:
    """Identity of a reference as the Python layer's `run_id` resolves it (`score`, `calibrate-live`,
    `fit-hazard`, `calibrate latency`: `queuetruth.score._io.resolve_ref`)."""
    from queuetruth.score._io import resolve_ref

    p = ref.get("path")
    if isinstance(p, str) and (got := _file_sha(p)) is not None:
        return got
    return resolve_ref(ref)


def resolve_core(ref: dict[str, Any]) -> str:
    """Identity of a reference as the Rust core's `run_id` resolves it (`zoo`, `calibrate`, `shadows sample`;
    crates/calib/src/refs.rs): a `dataset_id` as given; a directory by its `qt.dataset_manifest` `dataset_id`, else
    the sha256 of its `manifest.json` (an error without one); a file by its sha256."""
    from queuetruth.schemas.io import sha256_file

    if isinstance(ref.get("dataset_id"), str):
        return str(ref["dataset_id"])
    p = ref.get("path")
    if not isinstance(p, str):
        raise QtError("QT-CONFIG-001", f"cannot resolve reference {ref} locally")
    path = Path(p)
    if path.is_dir():
        manifest = path / "manifest.json"
        try:
            doc = json.loads(manifest.read_text(encoding="utf-8"))
        except OSError as e:
            raise QtError("QT-INPUT-001", f"{manifest}: {e.strerror or e}") from None
        except ValueError as e:
            raise QtError("QT-INPUT-002", f"{manifest}: {e}") from None
        did = doc.get("dataset_id") if isinstance(doc, dict) else None
        if doc.get("schema") == "qt.dataset_manifest" and isinstance(did, str):
            return did
        return sha256_file(manifest)
    got = _file_sha(p)
    if got is None:
        raise QtError("QT-INPUT-001", f"{p}: not found")
    return got


#: Kinds whose `run_id` the Rust core computes (their references resolve as `resolve_core`).
CORE_KINDS = frozenset({"zoo", "calibrate", "shadows-sample"})


def _resolver(kind: str, spec: Mapping[str, Any]) -> Callable[[dict[str, Any]], str]:
    if kind in CORE_KINDS and not (kind == "calibrate" and spec.get("kind") == "latency"):
        return resolve_core
    return resolve


def inputs_of(spec: dict[str, Any], kind: str = "score") -> tuple[dict[str, str], bool]:
    """({field path: identity} of every data reference of the spec, resolved as the `run_id` of a `kind` run
    resolves it; all resolved)."""
    res = _resolver(kind, spec)
    out: dict[str, str] = {}
    complete = True
    for key, ref in _refs(spec):
        try:
            out[key] = res(ref)
        except (QtError, OSError, ValueError, TypeError):
            complete = False
    return out, complete


def dataset_hash(inputs: Mapping[str, str]) -> str | None:
    """sha256 of the canonical JSON of `inputs`; None without inputs."""
    return _h(dict(inputs)) if inputs else None


# ---- summaries ------------------------------------------------------------------------------------------------------


def _as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, BaseModel):
        try:
            return obj.model_dump(mode="json", by_alias=True)
        except Exception:  # a model that cannot be dumped records no fields
            return {}
    return obj if isinstance(obj, dict) else {}


def _scalar(v: Any) -> Any:
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, str):
        return v[:MAX_METRIC_TEXT]
    return _NOT_SCALAR


_NOT_SCALAR = object()


def _pick(doc: dict[str, Any], keys: Iterable[str], prefix: str = "") -> dict[str, Any]:
    out = {}
    for k in keys:
        if k in doc:
            v = _scalar(doc[k])
            if v is not _NOT_SCALAR:
                out[prefix + k] = v
    return out


def _metrics(kind: str, s: dict[str, Any]) -> tuple[str | None, str | None, dict[str, Any]]:
    """(reported run_id, name, metrics) of a summary."""
    rid = s.get("run_id") if isinstance(s.get("run_id"), str) else None
    name: str | None = None
    m: dict[str, Any] = {}
    if kind == "zoo":
        m = _pick(s, ("n_shadows", "n_leaderboard", "n_excluded_hbt_312"))
        if isinstance(s.get("models"), list):
            m["n_models"] = len(s["models"])
        m.update(_leaderboard(s.get("leaderboard")))
    elif kind in ("calibrate", "calibrate-live"):
        name = s.get("family") if isinstance(s.get("family"), str) else None
        m = _pick(s, ("kind", "family", "param_name", "estimate", "loglik", "n_events", "n_shadows"))
        trust = s.get("trust")
        if isinstance(trust, dict) and isinstance(trust.get("verdict"), str):
            m["trust_verdict"] = trust["verdict"]
        for key, label in (("held_out", "held_out"), ("baseline_held_out", "baseline_held_out")):
            v = _first_brier(s.get(key))
            if v is not None:
                m[f"{label}_brier_mean"] = v
    elif kind == "fit-hazard":
        mid = s.get("model_id")
        if isinstance(mid, str):
            tail = mid.rsplit(":", 1)[-1]
            rid = tail if _HEX16.match(tail) else None
        m = _pick(s, ("model_id", "n_rows_train", "recalibration"))
        scores = s.get("scores_test")
        if isinstance(scores, dict):
            m.update(_pick(scores, sorted(scores), prefix="test_"))
    elif kind == "score":
        m = _pick(s, ("n_orders", "n_matched", "twin_match_rate"))
        verdict = s.get("verdict")
        if isinstance(verdict, dict):
            m.update(_pick(verdict, ("signed_bias", "gates_passed", "headline")))
    elif kind == "shadows-sample":
        m = _pick(s, ("n",))
    if name is not None and not _LABEL.match(name):
        name = None
    if rid is not None and not _HEX16.match(rid):
        rid = None
    return rid, name, m


def _brier_mean(entry: Any) -> float | None:
    """Mean Brier score over the horizons of a leaderboard or held-out entry (null horizons left out)."""
    b = entry.get("brier") if isinstance(entry, dict) else None
    if not isinstance(b, dict):
        return None
    xs = [float(v) for v in b.values() if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)]
    return sum(xs) / len(xs) if xs else None


def _first_brier(held: Any) -> float | None:
    if isinstance(held, dict) and len(held) == 1:
        return _brier_mean(next(iter(held.values())))
    return None


def _leaderboard(board: Any) -> dict[str, Any]:
    """The zoo's own selection: each model's mean Brier over the horizons (`brier_mean:<model_id>`), the model with
    the lowest (`best_model`) and its margin over the runner-up (`best_margin`)."""
    if not isinstance(board, list):
        return {}
    scores: dict[str, float] = {}
    for e in board:
        mid = e.get("model_id") if isinstance(e, dict) else None
        v = _brier_mean(e)
        if isinstance(mid, str) and v is not None:
            scores[mid[: MAX_METRIC_TEXT - 16]] = v
    out: dict[str, Any] = {f"brier_mean:{k}": v for k, v in sorted(scores.items())}
    ranked = sorted(scores.items(), key=lambda kv: (kv[1], kv[0]))
    if ranked:
        out["best_model"] = ranked[0][0]
    if len(ranked) > 1:
        out["best_margin"] = ranked[1][1] - ranked[0][1]
    return out


def _refusal(kind: str, s: dict[str, Any]) -> QtError | None:
    """The CLI's own refusal of a returned result (`score` that matched no order, a calibration that fitted no
    event): such a run has no outcome, so it is recorded as failed, as the user saw it."""
    if not s:
        return None
    from queuetruth import cli

    try:
        if kind == "score":
            return cli._not_scored(s)
        if kind in ("calibrate", "calibrate-live"):
            return cli._calibration_refused(s)
    except Exception:  # an unexpected summary shape: recorded as returned
        return None
    return None


def _spec_model(kind: str) -> type[BaseModel] | None:
    from queuetruth.schemas import specs as s

    return {
        "zoo": s.ZooSpec,
        "calibrate": s.CalibrationSpec,
        "calibrate-live": s.LiveCalibrationSpec,
        "fit-hazard": s.HazardFitSpec,
        "score": s.ScorecardSpec,
        "shadows-sample": s.ShadowSampleSpec,
    }.get(kind)


def _input_spec(kind: str, spec: Any, options: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The fully defaulted spec as given (keyword options that are spec fields folded in), or the raw dict when it
    does not validate; None when it is not a mapping."""
    if isinstance(spec, BaseModel):
        doc = spec.model_dump(mode="json", by_alias=True)
    elif isinstance(spec, Mapping):
        doc = dict(spec)
    else:
        return None
    model = _spec_model(kind)
    if model is not None:
        fields = set(model.model_fields) | {f.alias for f in model.model_fields.values() if f.alias}
        doc.update({k: v for k, v in (options or {}).items() if k in fields})
        try:
            return model.model_validate(doc).model_dump(mode="json", by_alias=True)
        except (ValidationError, ValueError, TypeError):
            pass
    try:
        canonical_json(doc)
    except (TypeError, ValueError):
        return None
    return doc


def _error_info(e: BaseException) -> ErrorInfo:
    if isinstance(e, QtError) and re.match(r"^QT-[A-Z]+-[0-9]{3}$", e.code or ""):
        return ErrorInfo(code=e.code, message=str(e.message)[:4096])
    return ErrorInfo(code="QT-ENGINE-001", message=f"{type(e).__name__}: {e}"[:4096])


def _validated(fields: dict[str, Any]) -> LedgerRecord:
    """A record (ledger_id / seq / prev set by the append); `QT-CONFIG-001` naming the first invalid field."""
    try:
        rec = LedgerRecord.model_validate(
            {"ledger_id": "0" * 32, "seq": 0, "prev": GENESIS_PREV, "recorded_at_ns": now_ns(), **fields}
        )
        canonical_json(rec.model_dump(mode="json"))
    except ValidationError as e:
        first = e.errors(include_url=False)[0] if e.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", ()))
        raise QtError("QT-CONFIG-001", f"ledger record: {loc + ': ' if loc else ''}{first.get('msg', '')}") from None
    except (TypeError, ValueError, ArithmeticError) as e:
        raise QtError("QT-CONFIG-001", f"ledger record: not JSON: {e}") from None
    return rec


# ---- recording ------------------------------------------------------------------------------------------------------


def _int_ns(i: int, x: Any) -> int:
    if isinstance(x, bool):
        raise QtError("QT-CONFIG-001", f"index[{i}]: {x!r} is not a time")
    if isinstance(x, int):
        return x
    if isinstance(x, str):  # ISO 8601, UTC when no offset
        try:
            x = datetime.datetime.fromisoformat(x.strip())
        except ValueError:
            raise QtError("QT-CONFIG-001", f"index[{i}]: {x!r} is not a time (ISO 8601, UTC ns or datetime)") from None
    if isinstance(x, datetime.datetime):
        dt = x if x.tzinfo is not None else x.replace(tzinfo=datetime.UTC)
        d = dt - _EPOCH
        return (d.days * 86_400 + d.seconds) * 1_000_000_000 + d.microseconds * 1_000
    if isinstance(x, datetime.date):
        return (x - _EPOCH.date()).days * 86_400 * 1_000_000_000
    value = getattr(x, "value", None)  # pandas.Timestamp
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if hasattr(x, "astype"):  # numpy.datetime64
        try:
            return int(x.astype("datetime64[ns]").astype("int64"))
        except (TypeError, ValueError):
            pass
    raise QtError("QT-CONFIG-001", f"index[{i}]: {x!r} is not a time (ISO 8601, UTC ns, datetime or date)")


_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)


def make_series(
    returns: Any = None,
    scores: Any = None,
    *,
    index: Any = None,
    higher_is_better: bool = True,
    periods_per_year: float | None = None,
) -> Series | None:
    """The validated `Series` of `returns` or `scores` (None without either); `QT-CONFIG-001` when invalid."""
    if returns is not None and scores is not None:
        raise QtError("QT-CONFIG-001", "a trial carries returns or scores, not both")
    if returns is None and scores is None:
        if periods_per_year is not None:
            raise QtError("QT-CONFIG-001", "periods_per_year: applies to a returns series; none given")
        if index is not None:
            raise QtError("QT-CONFIG-001", "index: applies to a series; none given")
        return None
    kind, vals = ("returns", returns) if returns is not None else ("scores", scores)
    ix = None
    if index is not None:
        if isinstance(index, (str, bytes)) or not isinstance(index, Iterable):
            raise QtError("QT-CONFIG-001", f"index: expected a sequence of times, got {type(index).__name__}")
        ix = [_int_ns(i, x) for i, x in enumerate(index)]
    try:
        return Series(
            kind=kind,  # type: ignore[arg-type]
            values=_values(kind, vals),
            index=ix,
            periods_per_year=periods_per_year,
            higher_is_better=higher_is_better,
        )
    except ValidationError as e:
        first = e.errors(include_url=False)[0] if e.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", ()))
        raise QtError("QT-CONFIG-001", f"{kind}: {loc + ': ' if loc else ''}{first.get('msg', '')}") from None


def _series_fields(series: Series | None) -> tuple[dict[str, Any], dict[str, bytes]]:
    if series is None:
        return {"series": None}, {}
    ref, blob = _series.encode(series)
    return {"series": ref}, {ref.sha256: blob}


def run_record(
    kind: str,
    *,
    spec: Any = None,
    summary: Any = None,
    error: BaseException | None = None,
    options: Mapping[str, Any] | None = None,
    started_at_ns: int | None = None,
    study: str | None = None,
    note: str | None = None,
    series: Series | None = None,
) -> Draft:
    """The record of one run of an entry point (not yet appended) and the series file it references. An ok run is
    identified from its summary's spec echo (SPEC 7.3: the document `run_id` was computed from) and data
    references; a failed run from the spec as given, with no data hashes and no trial id. A returned result the
    CLI refuses (`score` that matched no order, a calibration that fitted no event) is a failed run too."""
    if kind not in RUN_KINDS:
        raise QtError("QT-CONFIG-001", f"kind: {kind!r} is not one of {', '.join(RUN_KINDS)}")
    summ = _as_dict(summary)
    echo = summ.get("spec") if isinstance(summ.get("spec"), dict) else None
    if echo is not None and ECHO_ONLY.get(kind):
        echo = {k: v for k, v in echo.items() if k not in ECHO_ONLY[kind]}
    fields: dict[str, Any] = {
        "kind": kind,
        "status": "error" if error is not None else "ok",
        "study": current_study(study),
        "code": code_version(),
        "started_at_ns": started_at_ns,
        "note": note,
    }
    sfields, blobs = _series_fields(series)
    fields.update(sfields)
    if error is not None:
        doc = _input_spec(kind, spec, options)
        fields["spec_hash"] = spec_hash(kind, doc) if doc is not None else None
        fields["error"] = _error_info(error)
        return Draft(_validated(fields), blobs)
    doc = echo if echo is not None else _input_spec(kind, spec, options)
    rid, name, metrics = _metrics(kind, summ)
    inputs: dict[str, str] = {}
    if doc is not None:
        inputs, complete = inputs_of(doc, kind)
        fields["spec_hash"] = spec_hash(kind, doc)
        if rid is None and complete:
            rid = _recompute_run_id(kind, doc, inputs)
    refused = _refusal(kind, summ)
    if refused is not None:
        fields.update({"status": "error", "error": _error_info(refused)})
    fields.update(
        {
            "run_id": rid,
            "trial_id": rid if refused is None else None,
            "inputs": inputs,
            "dataset_hash": dataset_hash(inputs),
            "name": name,
            "metrics": metrics,
        }
    )
    return Draft(_validated(fields), blobs)


def _recompute_run_id(kind: str, doc: dict[str, Any], inputs: Mapping[str, str]) -> str | None:
    """`run_id` of an echo whose summary reports none (`shadows sample`): computed by the Rust core for the kinds
    it runs, else by `queuetruth.schemas.runid.run_id` with the inputs already resolved; None when that fails."""
    if _resolver(kind, doc) is resolve_core:
        try:
            from queuetruth import api

            rid = str(api.run_id(doc))
        except Exception:  # no compiled core, or the core cannot resolve the echo: unknown, never guessed
            return None
        return rid if _HEX16.match(rid) else None
    from queuetruth.schemas.arrow import ENGINE_VERSION

    refs = {canonical_json(r): inputs[k] for k, r in _refs(doc) if k in inputs}
    try:
        return _run_id(doc, ENGINE_VERSION, lambda r: refs[canonical_json(r)])
    except (KeyError, TypeError, ValueError):
        return None


def _off(what: str) -> None:
    log.warning("ledger: QT_LEDGER=off: %s not recorded", what)


def record(
    kind: str,
    *,
    spec: Any = None,
    summary: Any = None,
    error: BaseException | None = None,
    options: Mapping[str, Any] | None = None,
    started_at_ns: int | None = None,
    study: str | None = None,
    note: str | None = None,
    returns: Any = None,
    scores: Any = None,
    index: Any = None,
    higher_is_better: bool = True,
    periods_per_year: float | None = None,
    ledger: Ledger | str | os.PathLike[str] | None = None,
) -> LedgerRecord | None:
    """Records one run of `kind` (`zoo`, `calibrate`, `calibrate-live`, `fit-hazard`, `score`, `shadows-sample`)
    from its spec and summary (or the error it raised), optionally with a per-period `returns` or per-unit `scores`
    series of your own (`index`: period ends, UTC ns or datetimes), and returns the stored record; None (with a
    warning) with `QT_LEDGER=off`. The entry points of `queuetruth.api` record their runs themselves; call this
    for runs made another way, or to attach a series to a run."""
    if switch() == "off":
        _off(f"{kind} run")
        return None
    draft = run_record(
        kind,
        spec=spec,
        summary=summary,
        error=error,
        options=options,
        started_at_ns=started_at_ns,
        study=study,
        note=note,
        series=make_series(
            returns, scores, index=index, higher_is_better=higher_is_better, periods_per_year=periods_per_year
        ),
    )
    if ledger is None:
        doc = _as_dict(summary).get("spec")
        led = ledger_for(doc if isinstance(doc, dict) else _input_spec(kind, spec, options))
        if led is None:
            return None
    else:
        led = as_ledger(ledger)
    return led.append(draft.record, draft.blobs)


def _jsonable(v: Any) -> Any:
    """`v` with tuples as lists and numpy-style scalars / arrays as Python values (`.item()` / `.tolist()`)."""
    if isinstance(v, Mapping):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if v is None or type(v) in (str, int, float, bool):
        return v
    for t in (bool, int, float, str):  # subclasses (numpy float64, IntEnum, ...) as the plain type
        if isinstance(v, t):
            return t(v)
    for attr in ("tolist", "item"):
        fn = getattr(v, attr, None)
        if callable(fn):
            return _jsonable(fn())
    raise QtError("QT-CONFIG-001", f"ledger record: a value of type {type(v).__name__} is not JSON")


def _values(name: str, values: Any) -> list[float]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise QtError("QT-CONFIG-001", f"{name}: expected a sequence of numbers, got {type(values).__name__}")
    out = []
    for i, x in enumerate(values):
        if isinstance(x, bool):
            raise QtError("QT-CONFIG-001", f"{name}[{i}]: {x!r} is not a number")
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            raise QtError("QT-CONFIG-001", f"{name}[{i}]: {x!r} is not a number") from None
    return out


def dataset_identity(dataset: Any) -> dict[str, str]:
    """`inputs` of a variant's `dataset`: a path (`os.PathLike`, or a string starting with `@`) is identified by
    its content, as `run_id` identifies data (a dataset directory by its `dataset_id`, another directory by its
    manifest or its files, a file by its sha256) under `dataset`; any other string is a free label, kept under
    `dataset_label` because it is not bound to any data."""
    if dataset is None:
        return {}
    if isinstance(dataset, os.PathLike) or (isinstance(dataset, str) and dataset.startswith("@")):
        p = Path(os.fspath(dataset)[1:] if isinstance(dataset, str) else os.fspath(dataset))
        if not p.exists():
            raise QtError("QT-INPUT-001", f"dataset: {p}: not found")
        return {"dataset": resolve({"path": str(p)})}
    if not (isinstance(dataset, str) and dataset.strip()):
        raise QtError("QT-CONFIG-001", "dataset: a non-empty label, or a path (os.PathLike or '@PATH'), is expected")
    return {"dataset_label": dataset}


def variant_record(
    name: str,
    params: Mapping[str, Any] | None = None,
    *,
    returns: Any = None,
    scores: Any = None,
    index: Any = None,
    higher_is_better: bool = True,
    periods_per_year: float | None = None,
    study: str | None = None,
    dataset: Any = None,
    code_version_: str | None = None,
    run_ids: Iterable[str] = (),
    metrics: Mapping[str, Any] | None = None,
    note: str | None = None,
    started_at_ns: int | None = None,
) -> Draft:
    """The record of a user-registered variant (not yet appended) and its series file; see `register_variant`."""
    series = make_series(
        returns, scores, index=index, higher_is_better=higher_is_better, periods_per_year=periods_per_year
    )
    p = _jsonable(dict(params or {}))
    extra = _jsonable(dict(metrics or {}))
    for what, doc in (("params", p), ("metrics", extra)):
        try:
            canonical_json(doc)
        except (TypeError, ValueError, ArithmeticError) as e:
            raise QtError("QT-CONFIG-001", f"ledger record: {what}: not JSON: {e}") from None
    inputs = dataset_identity(dataset)
    dh = dataset_hash(inputs)
    if not (isinstance(name, str) and _LABEL.match(name)):
        raise QtError("QT-CONFIG-001", f"name: {name!r} must match {LABEL} (letters, digits, _ . : -; 1 to 128)")
    sfields, blobs = _series_fields(series)
    ref = sfields["series"]
    # the outcome is part of the identity: re-evaluating the same configuration after a code change that moved the
    # result is a new trial even when no code_version was given; an identical re-registration is the same trial
    outcome = {"series": ref.model_dump(mode="json")} if ref is not None else {"metrics": extra}
    fields = {
        "kind": "variant",
        "status": "ok",
        "study": current_study(study),
        "name": name,
        "params": p,
        "spec_hash": _h({"kind": "variant", "spec": {"name": name, "params": p}})[:16],
        "trial_id": _h({"name": name, "params": p, "dataset_hash": dh, "code": code_version_, "outcome": outcome})[:16],
        "inputs": inputs,
        "dataset_hash": dh,
        "metrics": extra,
        "run_ids": list(run_ids),
        "code": code_version(code_version_),
        "note": note,
        "started_at_ns": started_at_ns,
        **sfields,
    }
    return Draft(_validated(fields), blobs)


def register_variant(
    name: str,
    params: Mapping[str, Any] | None = None,
    *,
    returns: Any = None,
    scores: Any = None,
    index: Any = None,
    higher_is_better: bool = True,
    periods_per_year: float | None = None,
    study: str | None = None,
    dataset: Any = None,
    code_version: str | None = None,
    run_ids: Iterable[str] = (),
    metrics: Mapping[str, Any] | None = None,
    note: str | None = None,
    started_at_ns: int | None = None,
    ledger: Ledger | str | os.PathLike[str] | None = None,
) -> LedgerRecord | None:
    """Records one strategy variant you tried: its `name` and `params`, optionally its per-period `returns` or
    per-unit `scores` (with `index`, the period end of each value as UTC ns, ISO 8601 strings or datetimes, so
    that trials can be aligned), the `dataset` it was evaluated on (a path, `os.PathLike` or `'@PATH'`,
    identified by content; or a free label), your `code_version` and the `run_ids` of the queuetruth runs it
    used. Its trial id hashes (name, params, dataset, code version, outcome), the outcome being the series (or,
    without one, the metrics): the same evaluation registered twice is one trial, and a re-evaluation with a
    different result is a new one. Returns the stored record; None (with a warning) with `QT_LEDGER=off`.
    Register every variant you try, including the ones you discard: the trial count needs them."""
    if switch() == "off":
        _off(f"variant {name!r}")
        return None
    draft = variant_record(
        name,
        params,
        returns=returns,
        scores=scores,
        index=index,
        higher_is_better=higher_is_better,
        periods_per_year=periods_per_year,
        study=study,
        dataset=dataset,
        code_version_=code_version,
        run_ids=run_ids,
        metrics=metrics,
        note=note,
        started_at_ns=started_at_ns,
    )
    return as_ledger(ledger).append(draft.record, draft.blobs)


def register_variants(
    variants: Iterable[Mapping[str, Any]], *, ledger: Ledger | str | os.PathLike[str] | None = None
) -> list[LedgerRecord]:
    """Records many variants at once, in order, under one lock with one write and one fsync: each item holds the
    keyword arguments of `register_variant` (`name` required). Every item is validated before anything is
    written. Returns the stored records; [] (with a warning) with `QT_LEDGER=off`."""
    items = list(variants)
    if switch() == "off":
        _off(f"{len(items)} variant(s)")
        return []
    drafts = []
    for i, item in enumerate(items):
        if not isinstance(item, Mapping) or "name" not in item:
            raise QtError("QT-CONFIG-001", f"variants[{i}]: expected a mapping with a name")
        kw = dict(item)
        if "ledger" in kw:
            raise QtError("QT-CONFIG-001", f"variants[{i}]: ledger is given once, to register_variants")
        kw["code_version_"] = kw.pop("code_version", None)
        try:
            drafts.append(variant_record(**kw))
        except TypeError as e:
            raise QtError("QT-CONFIG-001", f"variants[{i}]: {e}") from None
    return as_ledger(ledger).append_many(drafts)


def _auto(
    kind: str,
    spec: Any,
    options: Mapping[str, Any] | None,
    result: Any,
    error: BaseException | None,
    t0: int,
) -> None:
    """Records a run of an entry point; any failure is logged, never raised."""
    try:
        draft = run_record(kind, spec=spec, summary=result, error=error, options=options, started_at_ns=t0)
        echo = _as_dict(result).get("spec")
        led = ledger_for(echo if isinstance(echo, dict) else _input_spec(kind, spec, options))
        if led is None:
            return
        stored = led.append(draft.record, draft.blobs)
        log.debug("ledger: recorded %s seq %d trial %s", kind, stored.seq, stored.trial_id)
    except QtError as e:
        log.warning("ledger: %s run not recorded: %s", kind, e)
    except Exception as e:  # recording is a side output: never fail the run
        log.warning("ledger: %s run not recorded: %s: %s", kind, type(e).__name__, e)


def recorded(kind: str) -> Callable[[F], F]:
    """Decorator of an entry point taking the spec first: after each outermost call (a nested recorded call, such
    as the zoo run inside `score`, is part of its caller's trial), records the run, or the error it raised, in the
    default ledger. The wrapped function's arguments, result and exceptions are passed through unchanged."""
    if kind not in RUN_KINDS:
        raise ValueError(f"recorded: unknown kind {kind!r}")

    def deco(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if _ACTIVE.get() or not auto_enabled():
                return fn(*args, **kwargs)
            spec = args[0] if args else kwargs.get("spec")
            options = {k: v for k, v in kwargs.items() if k != "spec"}
            token = _ACTIVE.set(True)
            t0 = now_ns()
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                _ACTIVE.reset(token)
                _auto(kind, spec, options, None, e, t0)
                raise
            except BaseException:
                _ACTIVE.reset(token)
                raise
            _ACTIVE.reset(token)
            _auto(kind, spec, options, result, None, t0)
            return result

        wrapper.__qt_ledger_kind__ = kind  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return deco


#: CLI commands the Rust core runs natively that are recorded, by command path.
NATIVE_KINDS: dict[tuple[str, ...], str] = {("shadows", "sample"): "shadows-sample"}


def run_native(cmd: dict[str, Any], runner: Callable[[dict[str, Any]], tuple[Any, bool]]) -> tuple[Any, bool]:
    """`runner(cmd)` (the CLI's native command runner), recording the run when its path is in `NATIVE_KINDS`."""
    kind = NATIVE_KINDS.get(tuple(cmd.get("path") or ()))
    if kind is None or _ACTIVE.get() or not auto_enabled():
        return runner(cmd)
    spec = {k: v for k, v in (cmd.get("args") or {}).items() if k != "_cli"}
    token = _ACTIVE.set(True)
    t0 = now_ns()
    try:
        doc, gate = runner(cmd)
    except Exception as e:
        _ACTIVE.reset(token)
        _auto(kind, spec, None, None, e, t0)
        raise
    except BaseException:
        _ACTIVE.reset(token)
        raise
    _ACTIVE.reset(token)
    _auto(kind, spec, None, doc, None, t0)
    return doc, gate
