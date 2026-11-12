"""The manifest of a dataset (`qt.dataset_manifest`, SPEC 3.3) or of a live log (`qt.live_manifest`, SPEC 3.8).

`Manifest` keeps the document as read: key order and fields this version does not know are kept, so a writer that
changes a few fields writes the rest back byte for byte. Every field a reader uses is checked on load and fails
`QT-INPUT-002` naming the file and the field.
"""

from __future__ import annotations

import copy
import datetime
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError
from queuetruth.integrity import sha256_text

MANIFEST_FILE = "manifest.json"
DATASET_SCHEMA = "qt.dataset_manifest"
LIVE_SCHEMA = "qt.live_manifest"

#: `price_exp` range of every queuetruth dataset (SPEC 1.2 `Price`; ingest refuses others).
PRICE_EXP_MIN, PRICE_EXP_MAX = -12, 0
#: date32 day numbers a `datetime.date` can hold (0001-01-01 .. 9999-12-31).
MIN_DAY, MAX_DAY = -719_162, 2_932_896
I64_MIN, I64_MAX = -(2**63), 2**63 - 1
_TEXT_FIELDS = ("kind", "source_format", "venue", "source_venue", "rule_set", "dataset_id", "validation_status")
_EPOCH = datetime.date(1970, 1, 1)


def date_str(days: int) -> str:
    """`YYYY-MM-DD` of a day number (days since 1970-01-01)."""
    return (_EPOCH + datetime.timedelta(days=int(days))).isoformat()


def day_number(value: Any) -> int:
    """Days since 1970-01-01 of a `datetime.date`, a `YYYY-MM-DD` string or a day number."""
    if isinstance(value, datetime.date):
        return (value - _EPOCH).days
    if isinstance(value, str) and "-" in value[1:]:
        return (datetime.date.fromisoformat(value) - _EPOCH).days
    return int(value)


def dataset_id(partition_shas: list[str], source_format: str) -> str:
    """SPEC 3.10 `dataset_id`: sha256 of the sorted partition-file sha256 list, newline-joined, then a newline and
    `source_format` (the rule of `qt_schema::json::dataset_id`)."""
    return sha256_text("\n".join(sorted(partition_shas)) + "\n" + source_format)


def bad(where: str | Path, msg: str, hint: str | None = None) -> QtError:
    """`QT-INPUT-002` for a file or a field of one."""
    return QtError("QT-INPUT-002", f"{where}: {msg}", hint)


def _no_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def _int(v: Any, where: str, lo: int, hi: int, *, optional: bool = False) -> int | None:
    """A JSON integer in [lo, hi] (bools are not integers)."""
    if v is None and optional:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise bad(where, f"expected an integer in [{lo}, {hi}], got {str(v)[:40]!r}")
    return v


def _list_of_dicts(v: Any, where: str) -> list[dict[str, Any]]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
        raise bad(where, "expected an array of objects")
    return v


def check_rel_path(rel: str, where: str) -> Path:
    """`rel` as a relative path made only of normal components, else `QT-INPUT-002` (a manifest must not make a
    reader open `/etc/passwd` or a sibling dataset). The check is lexical and mirrors the engine's (`qt_ingest`
    `check_rel_path`): no absolute path, drive, `..`, leading `.` or NUL. Symlinks are followed, as the engine
    follows them (datasets with partitions on another volume are linked in)."""
    p = Path(rel)
    if not rel or "\x00" in rel or p.is_absolute() or p.drive or p.root:
        raise bad(where, f"{rel[:80]!r} is not a relative path inside the dataset")
    if rel == "." or rel.startswith(("./", ".\\")) or ".." in p.parts:  # Rust's CurDir / ParentDir components
        raise bad(where, f"{rel[:80]!r} points outside the dataset directory")
    return p


@dataclass(frozen=True)
class PartitionFile:
    """One Parquet file of a partition (path relative to the dataset root)."""

    path: str
    rows: int | None
    sha256: str | None


@dataclass(frozen=True)
class Partition:
    """One (instrument, session date) partition of a dataset manifest."""

    instrument_id: int
    session_date: int
    files: tuple[PartitionFile, ...]
    rows: int | None
    ts_min: int | None
    ts_max: int | None

    @property
    def date(self) -> str:
        """The session date, `YYYY-MM-DD`."""
        return date_str(self.session_date)

    @property
    def key(self) -> tuple[int, int]:
        """`(instrument_id, session_date)`."""
        return (self.instrument_id, self.session_date)


def check_dataset_manifest(m: dict[str, Any], where: str | Path) -> None:
    """Checks every field of a `qt.dataset_manifest` that readers use (`QT-INPUT-002` naming the field)."""
    for k in _TEXT_FIELDS:
        if m.get(k) is not None and not isinstance(m[k], str):
            raise bad(f"{where}: {k}", "expected a string")
    for i, p in enumerate(_list_of_dicts(m.get("partitions"), f"{where}: partitions")):
        at = f"{where}: partitions[{i}]"
        for j, f in enumerate(_list_of_dicts(p.get("files"), f"{at}.files")):
            if not isinstance(f.get("path"), str) or not f["path"]:
                raise bad(f"{at}.files[{j}].path", "expected a relative file path")
            check_rel_path(f["path"], f"{at}.files")
            if f.get("sha256") is not None and not isinstance(f["sha256"], str):
                raise bad(f"{at}.files[{j}].sha256", "expected a string")
            _int(f.get("rows"), f"{at}.files[{j}].rows", 0, I64_MAX, optional=True)
        _int(p.get("instrument_id"), f"{at}.instrument_id", 0, 2**32 - 1)
        _int(p.get("session_date"), f"{at}.session_date", MIN_DAY, MAX_DAY)
        _int(p.get("rows"), f"{at}.rows", 0, I64_MAX, optional=True)
        _int(p.get("ts_min"), f"{at}.ts_min", I64_MIN, I64_MAX, optional=True)
        _int(p.get("ts_max"), f"{at}.ts_max", I64_MIN, I64_MAX, optional=True)
    for i, f in enumerate(_list_of_dicts(m.get("inputs"), f"{where}: inputs")):
        if f.get("path") is not None and not isinstance(f["path"], str):
            raise bad(f"{where}: inputs[{i}].path", "expected a string")
        _int(f.get("bytes"), f"{where}: inputs[{i}].bytes", 0, I64_MAX, optional=True)
        if f.get("sha256") is not None and not isinstance(f["sha256"], str):
            raise bad(f"{where}: inputs[{i}].sha256", "expected a string")
    _list_of_dicts(m.get("source_vintage"), f"{where}: source_vintage")
    pe = m.get("price_exp_by_instrument")
    if pe is not None:
        if not isinstance(pe, dict):
            raise bad(f"{where}: price_exp_by_instrument", "expected an object")
        for k, v in pe.items():
            _int(v, f"{where}: price_exp_by_instrument[{k}]", PRICE_EXP_MIN, PRICE_EXP_MAX, optional=True)


def check_live_manifest(m: dict[str, Any], where: str | Path) -> None:
    """Checks the fields of a `qt.live_manifest` that readers use."""
    for k in ("n_orders", "n_events"):
        _int(m.get(k), f"{where}: {k}", 0, I64_MAX, optional=True)
    for k in ("venues", "accounts"):
        v = m.get(k)
        if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) for x in v)):
            raise bad(f"{where}: {k}", "expected an array of strings")


class Manifest(Mapping[str, Any]):
    """A manifest document as read (a read-only mapping in file order), with typed views of the fields readers use.

    `to_dict()` returns a deep copy to change and write back with `DatasetWriter.finalize`."""

    def __init__(self, doc: dict[str, Any], path: Path | None = None, *, check: bool = True) -> None:
        self._doc = doc
        self.path = path
        where = str(path) if path is not None else MANIFEST_FILE
        if check:
            if self.schema == DATASET_SCHEMA:
                check_dataset_manifest(doc, where)
            elif self.schema == LIVE_SCHEMA:
                check_live_manifest(doc, where)
        self._partitions: tuple[Partition, ...] | None = None

    # ---- mapping ------------------------------------------------------------------------------------------------

    def __getitem__(self, key: str) -> Any:
        return self._doc[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._doc)

    def __len__(self) -> int:
        return len(self._doc)

    def __repr__(self) -> str:
        return f"Manifest({self.schema!r}, path={str(self.path)!r})"

    def to_dict(self) -> dict[str, Any]:
        """A deep copy of the document (key order kept)."""
        return copy.deepcopy(self._doc)

    # ---- typed fields -------------------------------------------------------------------------------------------

    @property
    def schema(self) -> str | None:
        """`qt.dataset_manifest`, `qt.live_manifest`, or None (no manifest, or another document)."""
        v = self._doc.get("schema")
        return v if isinstance(v, str) else None

    @property
    def is_dataset(self) -> bool:
        """A `qt.dataset_manifest`."""
        return self.schema == DATASET_SCHEMA

    @property
    def is_live(self) -> bool:
        """A `qt.live_manifest`."""
        return self.schema == LIVE_SCHEMA

    def _text(self, key: str, default: str = "") -> str:
        v = self._doc.get(key)
        return default if v is None else str(v)

    @property
    def kind(self) -> str:
        """The book kind of a dataset manifest, `mbo` or `l2` (`mbo` when absent, as v0.1 readers assumed)."""
        return self._text("kind", "mbo") or "mbo"

    @property
    def dataset_id(self) -> str:
        return self._text("dataset_id")

    @property
    def source_format(self) -> str:
        return self._text("source_format")

    @property
    def venue(self) -> str:
        """The venue (`generic` when absent)."""
        return self._text("venue", "generic") or "generic"

    @property
    def source_venue(self) -> str:
        """The source venue (the venue when absent)."""
        return self._text("source_venue") or self._text("venue")

    @property
    def rule_set(self) -> str:
        """The rule set the engines apply: `rule_set`, else the venue (v0.1), else `generic`."""
        return self._text("rule_set") or self._text("venue") or "generic"

    @property
    def validation_status(self) -> str:
        return self._text("validation_status", "synthetic_only") or "synthetic_only"

    @property
    def created_by(self) -> str:
        return self._text("created_by")

    @property
    def price_exp_by_instrument(self) -> dict[int, int | None]:
        pe = self._doc.get("price_exp_by_instrument") or {}
        return {int(k): (None if v is None else int(v)) for k, v in pe.items()} if isinstance(pe, dict) else {}

    @property
    def partitions(self) -> tuple[Partition, ...]:
        """The partitions in manifest order (entries without an instrument id or session date are skipped)."""
        if self._partitions is None:
            out = []
            for p in self._doc.get("partitions") or []:
                if not isinstance(p, dict) or "instrument_id" not in p or "session_date" not in p:
                    continue
                files = tuple(
                    PartitionFile(str(f["path"]), _opt_int(f.get("rows")), f.get("sha256"))
                    for f in p.get("files") or []
                    if isinstance(f, dict) and isinstance(f.get("path"), str)
                )
                out.append(
                    Partition(
                        int(p["instrument_id"]),
                        int(p["session_date"]),
                        files,
                        _opt_int(p.get("rows")),
                        _opt_int(p.get("ts_min")),
                        _opt_int(p.get("ts_max")),
                    )
                )
            self._partitions = tuple(out)
        return self._partitions

    def data_source(self) -> dict[str, str]:
        """`{dataset_id, venue, source_venue, validation_status}` for calibration provenance."""
        return {
            "dataset_id": self.dataset_id,
            "venue": self._text("venue"),
            "source_venue": self._text("source_venue", self._text("venue")),
            "validation_status": self.validation_status,
        }

    def model(self) -> Any:
        """The strict Pydantic model of the document (`DatasetManifest` or `LiveManifest`; `QT-INPUT-002` when it
        does not validate, for example because it has fields this version does not know)."""
        from pydantic import ValidationError

        from queuetruth.schemas.models import DatasetManifest, LiveManifest

        cls = {DATASET_SCHEMA: DatasetManifest, LIVE_SCHEMA: LiveManifest}.get(self.schema or "")
        if cls is None:
            raise bad(self.path or MANIFEST_FILE, f"not a {DATASET_SCHEMA} or {LIVE_SCHEMA} document")
        try:
            return cls.model_validate(self._doc)
        except ValidationError as e:
            raise bad(self.path or MANIFEST_FILE, f"invalid {self.schema}: {e.errors()[0].get('msg')}") from None


def _opt_int(v: Any) -> int | None:
    return None if v is None or isinstance(v, bool) else int(v)


def read_manifest_doc(
    root: str | Path,
    *,
    missing_ok: bool = False,
    contain: Callable[[Path], Path] | None = None,
    max_bytes: int | None = None,
) -> dict[str, Any] | None:
    """The `manifest.json` of a directory as a dict; None when it is absent and `missing_ok`. `QT-INPUT-001` when
    absent, `QT-INPUT-002` when it is not a JSON object (NaN and Infinity are refused)."""
    shown = Path(root) / MANIFEST_FILE
    if not shown.is_file():
        if missing_ok:
            return None
        raise QtError(
            "QT-INPUT-001",
            f"{Path(root)}: not a queuetruth dataset (no {MANIFEST_FILE})",
            "pass a directory written by `queuetruth ingest`, `queuetruth fixtures synth` or `queuetruth live import`",
        )
    real = contain(shown) if contain is not None else shown
    try:
        if max_bytes is not None and real.stat().st_size > max_bytes:
            raise bad(shown, f"larger than {max_bytes} bytes")
        doc = json.loads(real.read_text(encoding="utf-8"), parse_constant=_no_constant)
    except (OSError, ValueError, RecursionError) as e:
        raise bad(shown, f"unreadable manifest: {e}") from None
    if not isinstance(doc, dict):
        raise bad(shown, "not a JSON object")
    return doc
