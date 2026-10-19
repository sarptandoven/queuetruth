"""Table I/O shared by the scorer, the referee, live calibration and the exports.

Readers are tolerant: they accept any Parquet file with the needed columns (extra or missing optional columns are
fine), turn timestamps into integer ns and dictionary columns into strings. Writers cast to the `qt.*` schemas of
`queuetruth.schemas.arrow` and use the Parquet settings of SPEC 0.1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from queuetruth.errors import QtError
from queuetruth.schemas.arrow import DICT_VALUES, FULL_SCHEMAS, SCHEMAS
from queuetruth.schemas.io import sha256_file, write_table

Row = dict[str, Any]


def input_missing(path: str | Path, msg: str) -> QtError:
    """`QT-INPUT-001`."""
    return QtError("QT-INPUT-001", f"{path}: {msg}")


def input_format(location: str | Path, msg: str) -> QtError:
    """`QT-INPUT-002`."""
    return QtError("QT-INPUT-002", f"{location}: {msg}")


def config_error(msg: str, hint: str | None = None) -> QtError:
    """`QT-CONFIG-001`."""
    return QtError("QT-CONFIG-001", msg, hint)


def normalize(table: pa.Table) -> pa.Table:
    """Timestamps -> int64 ns, dictionaries -> their value type, large strings -> strings."""
    cols = []
    names = []
    for f in table.schema:
        col = table.column(f.name)
        t = f.type
        if pa.types.is_timestamp(t):
            col = col.cast(pa.timestamp("ns", tz=t.tz)).cast(pa.int64())
        elif pa.types.is_date32(t):
            col = col.cast(pa.int32())
        elif pa.types.is_dictionary(t):
            col = col.cast(t.value_type)
        cols.append(col)
        names.append(f.name)
    return pa.table(cols, names=names)


def rows(table: pa.Table) -> list[Row]:
    """Rows as dicts after `normalize`."""
    return normalize(table).to_pylist()


def read_parquet(path: str | Path, required: tuple[str, ...] = ()) -> pa.Table:
    """Reads a Parquet (or `.csv`) table and checks that `required` columns exist."""
    p = Path(path)
    if not p.exists():
        raise input_missing(p, "file not found")
    try:
        t = pacsv.read_csv(p) if p.suffix.lower() == ".csv" else pq.read_table(p)
    except (OSError, pa.ArrowInvalid) as e:
        raise input_format(p, str(e)) from e
    missing = [c for c in required if c not in t.column_names]
    if missing:
        raise input_format(p, f"missing columns {missing}")
    return t


def table_name(table: pa.Table) -> str | None:
    """`qt.schema` metadata of a table, if any."""
    meta = table.schema.metadata or {}
    v = meta.get(b"qt.schema")
    return v.decode() if v else None


def make_table(name: str, data: list[Row] | dict[str, list[Any]], *, full: bool = False) -> pa.Table:
    """A `qt.*` table from row dicts or columns (timestamps as int ns, dictionary values as strings). `full` writes
    the schema 1.1 layout (`FULL_SCHEMAS`: the v0.1 columns plus the 1.1 additions); the default is the v0.1 layout."""
    schema = (FULL_SCHEMAS if full else SCHEMAS)[name]
    if isinstance(data, list):
        cols = {f.name: [r.get(f.name) for r in data] for f in schema}
    else:
        cols = {f.name: list(data.get(f.name, [])) for f in schema}
    n = max((len(v) for v in cols.values()), default=0)
    arrays = []
    for f in schema:
        vals = cols[f.name]
        if len(vals) != n:
            if not vals:
                vals = [None] * n
            else:
                raise ValueError(f"{name}.{f.name}: {len(vals)} values for {n} rows")
        t = f.type
        if pa.types.is_dictionary(t):
            allowed = DICT_VALUES[name][f.name]
            index = {v: i for i, v in enumerate(allowed)}
            try:
                idx = [None if v is None else index[v] for v in vals]
            except KeyError as e:
                raise ValueError(f"{name}.{f.name}: value {e.args[0]!r} not in {list(allowed)}") from e
            arrays.append(
                pa.DictionaryArray.from_arrays(pa.array(idx, type=pa.int8()), pa.array(list(allowed), type=pa.utf8()))
            )
        elif pa.types.is_timestamp(t):
            arrays.append(pa.array(vals, type=pa.int64()).cast(t))
        elif pa.types.is_date32(t):
            arrays.append(pa.array(vals, type=pa.int32()).cast(t))
        else:
            arrays.append(pa.array(vals, type=t))
    return pa.Table.from_arrays(arrays, schema=schema)


def write(table: pa.Table, path: str | Path) -> dict[str, Any]:
    """Writes a `qt.*` table (SPEC 0.1 settings); returns its `Files` entry."""
    return write_table(table, path)


def write_json(doc: dict[str, Any], path: str | Path) -> None:
    """Pretty UTF-8 JSON with a trailing newline (no NaN)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def file_entry(path: str | Path, rows_: int | None = None) -> dict[str, Any]:
    """`Files` entry of an existing file."""
    p = Path(path)
    return {"name": p.name, "rows": rows_, "bytes": p.stat().st_size, "sha256": sha256_file(p)}


def ref_path(ref: Any, what: str) -> Path:
    """Local path of a `DatasetRef`/`FileRef` (`{"path": ...}` only; service ids are resolved by the worker)."""
    if ref is None:
        raise config_error(f"{what} is required")
    p = ref.get("path") if isinstance(ref, dict) else getattr(ref, "path", None)
    if not isinstance(p, str):
        raise config_error(f"{what}: a local path reference is required", 'use {"path": ...}')
    return Path(p)


def resolve_ref(ref: dict[str, Any]) -> str:
    """sha256 identity of a reference for `run_id` (SPEC 7.3): dataset -> `dataset_id`, live dir -> sha256 of its
    `manifest.json` (a directory without one: sha256 over its sorted relative paths and file hashes), file -> file
    sha256; other reference kinds hash their canonical text."""
    if "path" in ref:
        p = Path(ref["path"])
        if p.is_dir():
            m = p / "manifest.json"
            if m.exists():
                try:
                    doc = json.loads(m.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    doc = {}
                if isinstance(doc, dict) and isinstance(doc.get("dataset_id"), str):
                    return doc["dataset_id"]
                return sha256_file(m)
            import hashlib

            h = hashlib.sha256()
            for f in sorted(x for x in p.rglob("*") if x.is_file()):
                h.update(f"{f.relative_to(p).as_posix()}\n{sha256_file(f)}\n".encode())
            return h.hexdigest()
        if p.exists():
            return sha256_file(p)
        raise input_missing(p, "not found")
    import hashlib

    return hashlib.sha256(json.dumps(ref, sort_keys=True).encode()).hexdigest()


# ---- canonical live logs ------------------------------------------------------------------------------------------


def read_live_log(path: str | Path) -> tuple[list[Row], list[Row]]:
    """(orders, events) rows of a canonical live-log directory (`orders.parquet` + `events.parquet`, or `.csv`).

    Tolerant of simulator logs: only `order_key` is required in orders and `order_key`, `event_type` in events;
    events are sorted by (`order_key`, `event_seq`) when `event_seq` exists, else kept in file order per order.
    """
    p = Path(path)
    if not p.is_dir():
        raise input_missing(p, "live-log directory not found")
    for o, e in (("orders.parquet", "events.parquet"), ("orders.csv", "events.csv")):
        if (p / o).exists() and (p / e).exists():
            orders = rows(read_parquet(p / o, ("order_key",)))
            events = rows(read_parquet(p / e, ("order_key", "event_type")))
            break
    else:
        raise input_missing(p, "no orders.parquet/events.parquet (or orders.csv/events.csv)")
    pos = {id(ev): i for i, ev in enumerate(events)}
    events.sort(key=lambda ev: (str(ev["order_key"]), ev.get("event_seq") or 0, pos[id(ev)]))
    return orders, events


def read_manifest(path: str | Path) -> dict[str, Any]:
    """`manifest.json` of a directory as a dict ({} when absent or unreadable)."""
    m = Path(path) / "manifest.json"
    if not m.exists():
        return {}
    try:
        doc = json.loads(m.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


SIM_PREFIX = "sim:"


def sim_label(orders: list[Row], events: list[Row]) -> str | None:
    """`sim:<kind>` of a simulator log (E38, OC-9): the `venue` prefix `sim:` that `queuetruth.live.import_sim`
    writes on every order (first in sorted order), else an event `source` with that prefix (schema 1.1 `sim`
    source); None for a live log."""
    for vals in (
        sorted({str(o.get("venue") or "") for o in orders}),
        sorted({str(e.get("source") or "") for e in events}),
    ):
        hit = [v for v in vals if v.startswith(SIM_PREFIX) and len(v) > len(SIM_PREFIX)]
        if hit:
            return hit[0]
    return None
