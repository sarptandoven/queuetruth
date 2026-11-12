"""`open_dataset`: one way to open and read a queuetruth dataset directory or live log (module `queuetruth.dataset`)."""

from __future__ import annotations

import bisect
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from queuetruth.dataset._bounded import read_parquet as read_parquet_bounded
from queuetruth.dataset._instruments import INSTRUMENTS_FILE, Instrument, Session, instrument_rows
from queuetruth.dataset._manifest import (
    DATASET_SCHEMA,
    LIVE_SCHEMA,
    MANIFEST_FILE,
    Manifest,
    Partition,
    bad,
    check_rel_path,
    dataset_id,
    date_str,
    day_number,
    read_manifest_doc,
)
from queuetruth.errors import QtError
from queuetruth.schemas.io import sha256_file

DATA_QUALITY_FILE = "data_quality.json"
ORDERS_FILE = "orders.parquet"
EVENTS_FILE = "events.parquet"
#: `source_venue` of a prediction-market dataset (`queuetruth.pm`: an L2 dataset on the canonical YES ladder).
PREDICTION_MARKET_VENUES = frozenset({"kalshi", "polymarket"})
#: `venue` prefix of a simulator log (`queuetruth.live.import_sim`).
SIM_PREFIX = "sim:"

Contain = Callable[[Path], Path]


class DatasetKind(StrEnum):
    """What a directory holds."""

    #: An order-level (L3) dataset: the exact FIFO replay runs on it.
    MBO = "mbo"
    #: A price-level (L2) tape: simulation and markouts, no exact queue.
    L2 = "l2"
    #: A prediction-market dataset (`queuetruth.pm`): an L2 tape of binary markets.
    PREDICTION_MARKET = "prediction_market"
    #: A canonical live log (`orders` and `events`).
    LIVE = "live"
    #: A simulator log in the live-log layout (every venue `sim:<kind>`).
    SIM = "sim"


@dataclass(frozen=True)
class TableDef:
    """A logical table of a dataset: its `qt.schema` name and where its files are."""

    schema: str
    #: `partitions` (the manifest lists the files), `trades` (the `trades_source` tree), else the file name.
    where: str
    #: CSV fallback of a single-file table (live logs may be CSV).
    csv: str | None = None


TABLES: dict[str, TableDef] = {
    "instruments": TableDef("qt.instruments", INSTRUMENTS_FILE),
    "mbo": TableDef("qt.mbo", "partitions"),
    "l2": TableDef("qt.l2", "partitions"),
    "trades_source": TableDef("qt.trades", "trades"),
    "orders": TableDef("qt.live_orders", ORDERS_FILE, "orders.csv"),
    "events": TableDef("qt.live_events", EVENTS_FILE, "events.csv"),
}
_BY_SCHEMA = {d.schema: n for n, d in TABLES.items() if n not in ("mbo", "l2")} | {"qt.mbo": "mbo", "qt.l2": "l2"}


def table_schema_name(table: pa.Table | pa.Schema) -> str | None:
    """The `qt.schema` metadata of a table or schema, if any."""
    schema = table.schema if isinstance(table, pa.Table) else table
    v = (schema.metadata or {}).get(b"qt.schema")
    return v.decode(errors="replace") if v else None


def check_table_schema(table: pa.Table | pa.Schema, want: str, where: str | Path) -> None:
    """`QT-INPUT-002` when the table carries a `qt.schema` other than `want` (a table without one is accepted:
    CSV files and tables written by other tools carry none)."""
    got = table_schema_name(table)
    if got is not None and got != want:
        raise bad(
            where,
            f"is a {got} table where a {want} table belongs",
            "the file does not belong here: restore it from the source, or re-run the command that wrote the dataset",
        )


@dataclass
class Verification:
    """What `Dataset.verify` found: `problems` is empty when every file the manifest lists is there with the sha256,
    size and row count it records."""

    root: Path
    files_checked: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def raise_for_problems(self) -> None:
        """`QT-INPUT-002` naming the first problems."""
        if self.problems:
            more = f" (and {len(self.problems) - 3} more)" if len(self.problems) > 3 else ""
            raise QtError(
                "QT-INPUT-002",
                f"{self.root}: files differ from {MANIFEST_FILE}: {'; '.join(self.problems[:3])}{more}",
                "restore the files from the source, or re-create the dataset (`queuetruth ingest`, `data add`)",
            )


def _values(v: Any) -> list[Any]:
    if isinstance(v, (str, bytes, int)) or not isinstance(v, Iterable):
        return [v]
    return list(v)


class Dataset:
    """An opened dataset directory or live log. Open one with `open_dataset`.

    - `kind`: a `DatasetKind`; `book_kind` is `mbo` or `l2` for a book dataset, None for a log.
    - `manifest`: the `Manifest` (an empty one when the directory has none and it was opened with `missing_ok`).
    - `instruments`, `sessions`, `days`: the instrument table, one `Session` per instrument-day, the session dates.
    - `table(name, columns=None, filter=None)`: one logical table (`TABLES`), its `qt.schema` metadata checked.
    - `verify()`: the files against the sha256 and row counts the manifest records.
    """

    def __init__(
        self,
        root: Path,
        manifest: Manifest,
        kind: DatasetKind,
        *,
        contain: Contain | None = None,
        bounded: bool = False,
    ) -> None:
        self.root = root
        self.manifest = manifest
        self.kind = kind
        self._contain = contain
        self._bounded = bounded
        self._sessions: list[Session] | None = None
        self._by_iid: dict[int, list[Session]] | None = None

    def __repr__(self) -> str:
        return f"Dataset({str(self.root)!r}, kind={self.kind.value!r})"

    # ---- identity -----------------------------------------------------------------------------------------------

    @property
    def book_kind(self) -> str | None:
        """`mbo` or `l2` for a book dataset (a prediction-market dataset is `l2`), None for a log."""
        if self.kind in (DatasetKind.LIVE, DatasetKind.SIM):
            return None
        return self.manifest.kind

    @property
    def is_book(self) -> bool:
        return self.book_kind is not None

    @property
    def dataset_id(self) -> str:
        return self.manifest.dataset_id

    @property
    def partitions(self) -> tuple[Partition, ...]:
        return self.manifest.partitions

    def path(self, rel: str | Path) -> Path:
        """`root / rel`, through the `contain` hook when the dataset was opened with one."""
        p = self.root / rel
        return self._contain(p) if self._contain is not None else p

    # ---- instruments and sessions -------------------------------------------------------------------------------

    def instrument_rows(self) -> list[dict[str, Any]]:
        """Rows of `instruments.parquet` in file order (`session_date` as a day number; empty when absent)."""
        return [s.instrument.row for s in self.sessions]

    @property
    def sessions(self) -> list[Session]:
        """One `Session` per row of `instruments.parquet`, in file order (empty when the file is absent)."""
        if self._sessions is None:
            shown = self.root / INSTRUMENTS_FILE
            rows: list[dict[str, Any]] = []
            if shown.is_file():
                rows = instrument_rows(self._read_file(shown, "qt.instruments"), shown)
            self._sessions = [Session.from_row(r) for r in rows]
            by: dict[int, list[Session]] = {}
            for s in self._sessions:
                by.setdefault(s.instrument_id, []).append(s)
            self._by_iid = by
        return self._sessions

    @property
    def instruments(self) -> list[Instrument]:
        """One `Instrument` per instrument id, as of its first row in `instruments.parquet`."""
        self.sessions  # noqa: B018 (loads)
        assert self._by_iid is not None
        return [v[0].instrument for v in self._by_iid.values()]

    def sessions_of(self, instrument_id: int) -> list[Session]:
        """The instrument's sessions in file order."""
        self.sessions  # noqa: B018 (loads)
        assert self._by_iid is not None
        return list(self._by_iid.get(int(instrument_id), []))

    def session(self, instrument_id: int, session_date: Any) -> Session | None:
        """The instrument's session on a date (day number, `datetime.date` or `YYYY-MM-DD`)."""
        d = day_number(session_date)
        return next((s for s in self.sessions_of(instrument_id) if s.session_date == d), None)

    def session_at(self, instrument_id: int, ts: int) -> Session | None:
        """The session of the instrument holding `ts`: the last one whose local midnight is at or before `ts` (the
        first when `ts` is earlier); None without rows for the instrument."""
        v = sorted(self.sessions_of(instrument_id), key=lambda s: s.midnight_utc_ns or 0)
        if not v:
            return None
        mids = [s.midnight_utc_ns or 0 for s in v]
        return v[max(bisect.bisect_right(mids, ts) - 1, 0)]

    def instrument(self, instrument_id: int, *, ts: int | None = None, session_date: Any = None) -> Instrument | None:
        """The instrument's units on the session of `session_date`, else on the local day holding `ts`, else on its
        first session (None when the instrument has no row)."""
        v = self.sessions_of(instrument_id)
        if not v:
            return None
        if session_date is not None:
            s = self.session(instrument_id, session_date)
            if s is not None:
                return s.instrument
        if ts is not None:
            for s in v:
                if s.contains(ts):
                    return s.instrument
        return v[0].instrument

    @property
    def days(self) -> list[str]:
        """The session dates (`YYYY-MM-DD`, sorted) of the partitions, else of the instrument table."""
        days = {p.session_date for p in self.partitions} or {s.session_date for s in self.sessions}
        return [date_str(d) for d in sorted(days)]

    # ---- documents ----------------------------------------------------------------------------------------------

    def data_quality(self) -> dict[str, Any] | None:
        """`data_quality.json` as a dict; None when absent or unreadable."""
        shown = self.root / DATA_QUALITY_FILE
        if not shown.is_file():
            return None
        try:
            doc = json.loads(self.path(DATA_QUALITY_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return None
        return doc if isinstance(doc, dict) else None

    # ---- tables -------------------------------------------------------------------------------------------------

    def select(self, filter: Mapping[str, Any] | None = None) -> list[Partition]:
        """The partitions an `instrument_id` / `session_date` filter keeps (all without one)."""
        parts = list(self.partitions)
        if not filter:
            return parts
        if "instrument_id" in filter:
            ids = {int(x) for x in _values(filter["instrument_id"])}
            parts = [p for p in parts if p.instrument_id in ids]
        if "session_date" in filter:
            days = {day_number(x) for x in _values(filter["session_date"])}
            parts = [p for p in parts if p.session_date in days]
        return parts

    def files(self, name: str, filter: Mapping[str, Any] | None = None) -> list[Path]:
        """The files of a logical table (paths as shown, under `root`); a partitioned table follows the manifest
        order (`QT-INPUT-001` for a listed file that is missing)."""
        d = self._table_def(name)
        if d.where == "partitions":
            out = []
            for p in self.select(filter):
                for f in p.files:
                    path = self.root / check_rel_path(f.path, f"{self.root / MANIFEST_FILE}: partitions.files")
                    if not path.is_file():
                        raise QtError(
                            "QT-INPUT-001",
                            f"{path}: partition file missing ({MANIFEST_FILE} lists it)",
                            "restore the file, or re-create the dataset",
                        )
                    out.append(path)
            return out
        if d.where == "trades":
            base = self.root / "trades_source"
            if not base.is_dir():
                return []
            out = []
            keep = {(p.date, p.instrument_id) for p in self.select(filter)} if filter else None
            for f in sorted(base.glob("date=*/instrument_id=*/part-*.parquet")):
                key = (f.parent.parent.name.split("=", 1)[1], int(f.parent.name.split("=", 1)[1]))
                if keep is None or key in keep:
                    out.append(f)
            return out
        for fname in (d.where, d.csv):
            if fname and (self.root / fname).is_file():
                return [self.root / fname]
        return []

    def table(
        self,
        name: str,
        columns: list[str] | tuple[str, ...] | None = None,
        filter: Mapping[str, Any] | pc.Expression | None = None,
    ) -> pa.Table:
        """A logical table (`instruments`, `mbo`, `l2`, `trades_source`, `orders`, `events`; `qt.<name>` works
        too), its files concatenated in order and each one's `qt.schema` metadata checked (`QT-INPUT-002` for a
        file of another table). `columns` selects columns (`QT-INPUT-002` when one is missing). `filter` is a
        `pyarrow.compute` expression, or a mapping of column to a value or a collection of values; for partitioned
        tables its `instrument_id` and `session_date` keys select partitions. A table without files is empty, with
        the table's columns."""
        d = self._table_def(name)
        mapping = filter if isinstance(filter, Mapping) else None
        expr = None if isinstance(filter, Mapping) else filter
        paths = self.files(name, mapping if d.where in ("partitions", "trades") else None)
        if mapping and d.where not in ("partitions", "trades"):
            expr = self._expr(mapping, d.schema)
        tables = [self._read_file(p, d.schema, columns, expr) for p in paths]
        if not tables:
            from queuetruth.schemas.arrow import SCHEMAS

            schema = SCHEMAS[d.schema]
            if columns is not None:
                schema = pa.schema([schema.field(c) for c in columns if c in schema.names], metadata=schema.metadata)
            return schema.empty_table()
        if len(tables) == 1:
            return tables[0]
        try:
            return pa.concat_tables(tables)
        except (ValueError, pa.ArrowException) as e:
            raise bad(paths[0], f"its files differ in their columns: {e}") from None

    def _table_def(self, name: str) -> TableDef:
        key = _BY_SCHEMA.get(name, name)
        if key not in TABLES:
            raise QtError("QT-CONFIG-001", f"unknown dataset table {name!r}", f"one of {sorted(TABLES)}")
        return TABLES[key]

    @staticmethod
    def _expr(mapping: Mapping[str, Any], schema_name: str) -> pc.Expression | None:
        out = None
        for k, v in mapping.items():
            vals = _values(v)
            if k == "session_date" and schema_name == "qt.instruments":
                arr = pa.array([day_number(x) for x in vals], pa.int32()).cast(pa.date32())
                e = pc.field(k).isin(arr)
            else:
                e = pc.field(k).isin(vals)
            out = e if out is None else out & e
        return out

    def _read_file(
        self,
        shown: Path,
        want: str,
        columns: list[str] | tuple[str, ...] | None = None,
        expr: pc.Expression | None = None,
    ) -> pa.Table:
        real = self._contain(shown) if self._contain is not None else shown
        if real.suffix.lower() == ".csv":
            try:
                t = pacsv.read_csv(real)
            except (OSError, pa.ArrowInvalid) as e:
                raise bad(shown, f"unreadable: {e}") from None
        else:
            try:
                schema = pq.read_schema(real)
            except (OSError, ValueError, pa.ArrowException) as e:
                raise bad(shown, f"unreadable: {e}") from None
            check_table_schema(schema, want, shown)
            if columns is not None:
                missing = [c for c in columns if c not in schema.names]
                if missing:
                    raise bad(shown, f"missing columns {missing}", f"a {want} table has them; re-create the file")
            if self._bounded:
                t = read_parquet_bounded(real, columns=None if expr is not None else list(columns or []) or None)
            else:
                try:
                    return pq.read_table(real, columns=list(columns) if columns is not None else None, filters=expr)
                except (OSError, ValueError, pa.ArrowException) as e:
                    raise bad(shown, f"unreadable: {e}") from None
        check_table_schema(t, want, shown)
        if expr is not None:
            t = t.filter(expr)
        if columns is not None:
            missing = [c for c in columns if c not in t.column_names]
            if missing:
                raise bad(shown, f"missing columns {missing}", f"a {want} table has them; re-create the file")
            t = t.select(list(columns))
        return t

    # ---- integrity ----------------------------------------------------------------------------------------------

    def verify(self) -> Verification:
        """Checks every file the manifest records against it: present, sha256, size where recorded, row count (from
        the Parquet footer), each partition's total rows and the `dataset_id` (SPEC 3.10); for a live log, the
        order and event counts. Nothing raises; see `Verification.raise_for_problems`."""
        v = Verification(self.root)
        m = self.manifest
        if m.is_dataset or (m.schema is None and m.partitions):
            shas: list[str] = []
            for p in m.partitions:
                total = 0
                for f in p.files:
                    v.files_checked += 1
                    try:
                        rel = check_rel_path(f.path, "partition file")
                    except QtError as e:
                        v.problems.append(e.message)
                        continue
                    path = self.root / rel
                    if not path.is_file():
                        v.problems.append(f"{f.path}: listed in {MANIFEST_FILE} but missing")
                        continue
                    real = self.path(rel)
                    got = sha256_file(real)
                    if f.sha256 is None:
                        v.problems.append(f"{f.path}: {MANIFEST_FILE} records no sha256")
                    elif got != f.sha256:
                        v.problems.append(f"{f.path}: sha256 {got[:16]}... is not the {f.sha256[:16]}... recorded")
                    shas.append(got)
                    try:
                        n = pq.read_metadata(real).num_rows
                    except (OSError, ValueError, pa.ArrowException) as e:
                        v.problems.append(f"{f.path}: unreadable: {e}")
                        continue
                    total += n
                    if f.rows is not None and n != f.rows:
                        v.problems.append(f"{f.path}: {n} rows, {MANIFEST_FILE} records {f.rows}")
                if p.rows is not None and total != p.rows and not any(f.path in x for f in p.files for x in v.problems):
                    v.problems.append(f"partition {p.instrument_id} on {p.date}: {total} rows, {p.rows} recorded")
            if m.dataset_id and not v.problems:
                did = dataset_id(shas, m.source_format)
                if did != m.dataset_id:
                    v.problems.append(f"dataset_id {m.dataset_id} does not re-derive (got {did})")
        elif m.is_live:
            for name, key in (("orders", "n_orders"), ("events", "n_events")):
                paths = self.files(name)
                want = m.get(key)
                if not paths:
                    v.problems.append(f"{TABLES[name].where}: missing")
                    continue
                v.files_checked += 1
                if paths[0].suffix == ".csv":
                    n = self.table(name).num_rows
                else:
                    n = pq.read_metadata(self.path(paths[0].relative_to(self.root))).num_rows
                if want is not None and n != int(want):
                    v.problems.append(f"{paths[0].name}: {n} rows, {MANIFEST_FILE} records {key} = {want}")
        return v

    def describe(self) -> dict[str, Any]:
        """A short summary: path, identity, kind, partitions, records, dates and symbols."""
        m = self.manifest
        parts = self.partitions
        return {
            "path": str(self.root),
            "dataset_id": m.get("dataset_id"),
            "kind": self.kind.value,
            "source_format": m.get("source_format"),
            "venue": m.get("venue"),
            "source_venue": m.get("source_venue"),
            "rule_set": m.get("rule_set"),
            "validation_status": m.get("validation_status"),
            "partitions": len(parts),
            "records": sum(p.rows or 0 for p in parts),
            "session_dates": self.days,
            "symbols": sorted({i.label for i in self.instruments}),
        }


def _live_kind(root: Path, manifest: Manifest, contain: Contain | None) -> DatasetKind:
    venues = manifest.get("venues")
    if not isinstance(venues, list):
        p = root / ORDERS_FILE
        venues = []
        if p.is_file():
            try:
                real = contain(p) if contain is not None else p
                if "venue" in pq.read_schema(real).names:
                    venues = sorted({str(v) for v in pq.read_table(real, columns=["venue"]).column(0).to_pylist()})
            except (OSError, ValueError, pa.ArrowException):
                venues = []
    hit = [v for v in venues if isinstance(v, str) and v.startswith(SIM_PREFIX) and len(v) > len(SIM_PREFIX)]
    return DatasetKind.SIM if venues and len(hit) == len(venues) else DatasetKind.LIVE


def open_dataset(
    path: str | Path,
    *,
    missing_ok: bool = False,
    contain: Contain | None = None,
    bounded: bool = False,
    max_manifest_bytes: int | None = None,
) -> Dataset:
    """Opens a queuetruth dataset directory (an MBO or L2 dataset, a prediction-market dataset) or a live or
    simulator log, and checks its manifest.

    - `missing_ok`: a directory without `manifest.json` opens with an empty manifest (a log by its `orders` and
      `events` files, else an MBO dataset, as the scorer reads one). Otherwise it fails `QT-INPUT-001`.
    - `contain`: called with every path before it is read and returns the real path to open (the MCP local mode
      keeps reads inside its roots; the sandbox pins the files it resolved at open).
    - `bounded`: Parquet files are read with `read_parquet_bounded` (untrusted inputs: `queuetruth dq`).

    Errors: `QT-INPUT-001` (no such directory, no manifest), `QT-INPUT-002` (a manifest or field that does not
    parse, a path in it leading outside the directory, a file of another table)."""
    root = Path(path)
    if not root.is_dir():
        raise QtError("QT-INPUT-001", f"{root}: dataset directory not found", "check the path")
    if contain is not None:
        contain(root)
    doc = read_manifest_doc(root, missing_ok=missing_ok, contain=contain, max_bytes=max_manifest_bytes)
    manifest = Manifest(doc or {}, root / MANIFEST_FILE)
    if manifest.is_live:
        kind = _live_kind(root, manifest, contain)
    elif manifest.is_dataset or (doc is not None and manifest.schema is None):
        k = manifest.kind
        if k == "l2":
            kind = (
                DatasetKind.PREDICTION_MARKET if manifest.source_venue in PREDICTION_MARKET_VENUES else DatasetKind.L2
            )
        elif k == "mbo":
            kind = DatasetKind.MBO
        else:
            raise bad(f"{root / MANIFEST_FILE}: kind", f"{k!r} is not mbo or l2")
    elif doc is None:
        has_log = any((root / n).is_file() for n in (ORDERS_FILE, "orders.csv"))
        kind = _live_kind(root, manifest, contain) if has_log else DatasetKind.MBO
    else:
        raise bad(
            root / MANIFEST_FILE,
            f"a {manifest.schema} document, not a {DATASET_SCHEMA} or {LIVE_SCHEMA}",
            "pass a dataset directory or a live log",
        )
    return Dataset(root, manifest, kind, contain=contain, bounded=bounded)
