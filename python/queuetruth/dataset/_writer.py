"""`DatasetWriter`: one way to write a queuetruth dataset or live log (module `queuetruth.dataset`)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from queuetruth.dataset._manifest import MANIFEST_FILE, dataset_id
from queuetruth.integrity import sha256_file
from queuetruth.schemas.io import write_table

TMP_SUFFIX = ".tmp"


def _dump(doc: Any) -> dict[str, Any]:
    if hasattr(doc, "model_dump"):
        return doc.model_dump(mode="json", by_alias=True)
    return doc


def json_text(
    doc: Any, *, indent: int | None = 2, ensure_ascii: bool = True, sort_keys: bool = False, allow_nan: bool = False
) -> str:
    """The text a writer stores for a JSON document (a dict or a Pydantic model): `json.dumps` with these options
    and a trailing newline (NaN and Infinity refused unless `allow_nan`)."""
    return (
        json.dumps(_dump(doc), indent=indent, ensure_ascii=ensure_ascii, sort_keys=sort_keys, allow_nan=allow_nan)
        + "\n"
    )


def write_text_atomic(path: Path, text: str) -> None:
    """Writes `text` to `path` through a temporary name in the same directory and a rename, so a reader sees the
    old file or the new one, never a part."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TMP_SUFFIX)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class DatasetWriter:
    """Writes the files of a dataset directory, records each file's `Files` entry (`name`, `rows`, `bytes`,
    `sha256`) and finally the manifest, atomically (`finalize`: a temporary name, then a rename), so a directory
    with a manifest is always complete.

    `name` in an entry is the path relative to the root (`instruments.parquet`, `mbo/date=.../part-00000.parquet`).
    Tables are written with the Parquet settings of SPEC 0.1 (`queuetruth.schemas.io.write_table`)."""

    def __init__(self, root: str | Path, *, exist_ok: bool = True) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=exist_ok)
        self._entries: dict[str, dict[str, Any]] = {}

    # ---- files --------------------------------------------------------------------------------------------------

    def _rel(self, rel: str | Path) -> str:
        return Path(rel).as_posix()

    def write_table(self, rel: str | Path, table: pa.Table, schema: pa.Schema | None = None) -> dict[str, Any]:
        """Writes a `qt.*` table (cast to `schema` when given) and returns its entry."""
        r = self._rel(rel)
        entry = write_table(table, self.root / r, schema)
        entry["name"] = r
        self._entries[r] = entry
        return dict(entry)

    def write_json(self, rel: str | Path, doc: Any, *, indent: int | None = 2, ensure_ascii: bool = True) -> dict:
        """Writes a JSON document (atomically) and returns its entry."""
        r = self._rel(rel)
        write_text_atomic(self.root / r, json_text(doc, indent=indent, ensure_ascii=ensure_ascii))
        return self.record(r)

    def copy(self, src: str | Path, rel: str | Path, *, link: bool = False) -> dict[str, Any]:
        """Copies a file into the dataset (`link`: a hard link when the file system allows one, else a copy) and
        returns its entry."""
        r = self._rel(rel)
        dst = self.root / r
        dst.parent.mkdir(parents=True, exist_ok=True)
        if link:
            try:
                os.link(src, dst)
            except OSError:  # another file system, or links not allowed: copy
                shutil.copy2(src, dst)
        else:
            shutil.copy2(src, dst)
        return self.record(r)

    def copy_tree(self, src: str | Path, rel: str | Path) -> None:
        """Copies a directory tree into the dataset (files not recorded: trees such as `trades_source` are not
        listed in the manifest)."""
        shutil.copytree(src, self.root / rel, dirs_exist_ok=True)

    def record(self, rel: str | Path, rows: int | None = None) -> dict[str, Any]:
        """Records a file already written under the root (by the engine, say) and returns its entry; `rows` is read
        from the Parquet footer when not given."""
        r = self._rel(rel)
        p = self.root / r
        if rows is None and p.suffix == ".parquet":
            rows = pq.read_metadata(p).num_rows
        entry = {"name": r, "rows": rows, "bytes": p.stat().st_size, "sha256": sha256_file(p)}
        self._entries[r] = entry
        return dict(entry)

    def entry(self, rel: str | Path) -> dict[str, Any] | None:
        """The recorded entry of a file, if any."""
        e = self._entries.get(self._rel(rel))
        return dict(e) if e is not None else None

    @property
    def entries(self) -> list[dict[str, Any]]:
        """Every recorded entry, in the order written."""
        return [dict(e) for e in self._entries.values()]

    # ---- the manifest -------------------------------------------------------------------------------------------

    def update_partitions(self, manifest: dict[str, Any]) -> list[str]:
        """Sets the `sha256` and `rows` of every partition file of `manifest` that this writer recorded and each
        partition's `rows` (the sum over its files); returns every partition file's sha256 (for `dataset_id`)."""
        shas = []
        for p in manifest.get("partitions") or []:
            touched = False
            for f in p.get("files") or []:
                e = self._entries.get(self._rel(f["path"]))
                if e is not None:
                    f["sha256"] = e["sha256"]
                    if e.get("rows") is not None:
                        f["rows"] = e["rows"]
                    touched = True
                shas.append(f["sha256"])
            if touched:
                p["rows"] = sum(int(f.get("rows") or 0) for f in p.get("files") or [])
        return shas

    @staticmethod
    def dataset_id(manifest: dict[str, Any]) -> str:
        """SPEC 3.10 `dataset_id` of a manifest's partition files and `source_format`."""
        shas = [f["sha256"] for p in manifest.get("partitions") or [] for f in p.get("files") or []]
        return dataset_id(shas, str(manifest.get("source_format", "")))

    def finalize(
        self,
        manifest: Any,
        *,
        indent: int | None = 2,
        ensure_ascii: bool = True,
        name: str = MANIFEST_FILE,
    ) -> dict[str, Any]:
        """Writes the manifest (a dict, kept in its key order, or a Pydantic model) last and atomically; returns its
        entry."""
        write_text_atomic(self.root / name, json_text(manifest, indent=indent, ensure_ascii=ensure_ascii))
        return self.record(name)
