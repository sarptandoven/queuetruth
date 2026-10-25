"""Fuzz properties of `queuetruth dq`: tiny MBO datasets with random records (every action and side, prices and
sizes at the type limits, unordered times and sequences), then hostile metadata (the manifest, `instruments.parquet`,
`data_quality.json`) and damaged partition files. `dq.run` must report or fail with a `QT-*` code."""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from dq.helpers import make_dataset, midnight_ny, rec
from fuzz._harness import FUZZ, guarded, scaled, workdir
from fuzz._values import nasty
from hypothesis import given
from hypothesis import strategies as st

from queuetruth import dq
from queuetruth.dq.data import UNDEF_PRICE, Dataset

DATES = ["2026-06-15", "2026-06-16"]
I64 = 2**63 - 1
U64 = 2**64 - 1


@st.composite
def records(draw: st.DrawFn, date: str, hostile: bool) -> list[dict[str, Any]]:
    open_ns = midnight_ny(date) + 34_200 * 10**9
    out = []
    t = open_ns - draw(st.integers(0, 10**9))
    for seq in range(draw(st.integers(0, 40))):
        t += draw(st.integers(-(10**6) if hostile else 0, 10**9))
        price = draw(
            st.one_of(
                st.integers(90, 110).map(lambda p: p * 10_000_000),
                st.sampled_from([UNDEF_PRICE, 0, -1, I64 - 1, -(2**63), 12_345] if hostile else [100 * 10_000_000]),
            )
        )
        size = draw(st.one_of(st.integers(0, 500), st.sampled_from([0, I64] if hostile else [1])))
        out.append(
            rec(
                t,
                draw(st.sampled_from("ACMRTFN" + ("XZ" if hostile else ""))),
                draw(st.sampled_from("BAN" + ("Q" if hostile else ""))),
                price,
                size,
                draw(st.one_of(st.integers(1, 12), st.sampled_from([0, I64] if hostile else [1]))),
                draw(st.one_of(st.just(seq + 1), st.integers(0, 50) if hostile else st.just(seq + 1))),
                flags=draw(st.sampled_from([128, 0, 32, 160, 8, 4, 255] if hostile else [128, 0, 160])),
                ts_recv=t + draw(st.integers(-(10**6) if hostile else 0, 10**7)),
            )
        )
    return out


@st.composite
def dataset(draw: st.DrawFn, *, hostile: bool) -> dict[str, Any]:
    parts = {}
    for date in draw(st.lists(st.sampled_from(DATES), min_size=1, max_size=2, unique=True)):
        for iid in draw(st.lists(st.sampled_from([1, 2]), min_size=1, max_size=2, unique=True)):
            parts[(date, iid)] = draw(records(date, hostile))
    venue = draw(st.sampled_from(["nasdaq", "cme", "coinbase", "generic", "crypto_l3", "iex", "unknown"]))
    tick = draw(st.sampled_from([10_000_000, 1, 0, -5] if hostile else [10_000_000, 1]))
    return {"parts": parts, "venue": venue, "tick": tick}


MANIFEST_PATHS = [
    (),
    ("schema",),
    ("kind",),
    ("partitions",),
    ("partitions", 0),
    ("partitions", 0, "instrument_id"),
    ("partitions", 0, "session_date"),
    ("partitions", 0, "files"),
    ("partitions", 0, "files", 0),
    ("partitions", 0, "files", 0, "path"),
    ("partitions", 0, "rows"),
    ("partitions", 0, "ts_min"),
    ("venue",),
    ("rule_set",),
    ("source_format",),
    ("inputs",),
]
PATH_VALUES = ["/etc/passwd", "../../../etc/hosts", "..", "", "mbo", "manifest.json", "instruments.parquet", "\x00"]


def _set(doc: Any, path: tuple[Any, ...], value: Any) -> Any:
    if not path:
        return value
    cur = doc
    for k in path[:-1]:
        try:
            cur = cur[k]
        except (KeyError, IndexError, TypeError):
            return doc
    try:
        cur[path[-1]] = value
    except (KeyError, IndexError, TypeError):
        pass
    return doc


@st.composite
def damage(draw: st.DrawFn) -> list[tuple[str, Any]]:
    """Damage to apply after the dataset is written: manifest edits, replaced files, deleted files."""
    out: list[tuple[str, Any]] = []
    for _ in range(draw(st.integers(1, 3))):
        kind = draw(
            st.sampled_from(
                [
                    "manifest",
                    "manifest",
                    "manifest_path",
                    "instruments",
                    "instrument_values",
                    "dq_json",
                    "partition",
                    "partition_types",
                    "flip",
                    "flip",
                    "delete",
                ]
            )
        )
        if kind == "flip":  # a few bytes of a Parquet file overwritten (footer, page headers, column names)
            target = draw(st.sampled_from(["instruments.parquet", "partition"]))
            flips = draw(
                st.lists(st.tuples(st.floats(0, 1, exclude_max=True), st.integers(0, 255)), min_size=1, max_size=4)
            )
            out.append((kind, (target, flips)))
        elif kind == "instrument_values":
            out.append((kind, draw(st.dictionaries(st.sampled_from(sorted(INSTRUMENT_EDGES)), st.integers(0, 4)))))
        elif kind == "partition_types":
            out.append((kind, draw(st.sampled_from(sorted(_RETYPE_COLUMN)))))
        elif kind == "manifest":
            out.append((kind, (draw(st.sampled_from(MANIFEST_PATHS)), draw(nasty))))
        elif kind == "manifest_path":
            out.append(("manifest", (("partitions", 0, "files", 0, "path"), draw(st.sampled_from(PATH_VALUES)))))
        elif kind in ("instruments", "dq_json", "partition"):
            out.append(
                (kind, draw(st.one_of(st.binary(max_size=64), st.sampled_from(["copy_manifest", "truncate", "json"]))))
            )
        else:
            out.append((kind, draw(st.sampled_from(["instruments.parquet", "data_quality.json", "partition"]))))
    return out


#: Extreme but well-typed instrument values (index chosen by the strategy).
INSTRUMENT_EDGES: dict[str, list[Any]] = {
    "price_exp": [-128, 127, -4, 0, -12],
    "session_date": [-(2**31), 2**31 - 1, -719_163, 2_932_897, 20_619],
    "tick_size": [0, -1, 2**63 - 1, 1, 7],
    "lot_size": [0, 2**64 - 1, 3, 1, 100],
    "midnight_utc_ns": [-(2**63), 2**63 - 1, 0, -1, 1],
    "open_s": [0, 2**32 - 1, 86_400, 57_600, 1],
    "close_s": [0, 2**32 - 1, 86_400, 34_200, 1],
}


#: Column each `partition_types` edit changes.
_RETYPE_COLUMN = {
    "string_price": "price",
    "null_size": "size",
    "drop_action": "action",
    "float_ts": "ts_event",
    "neg_size": "size",
}


def _first_part(root: Path) -> Path | None:
    parts = sorted(root.glob("mbo/date=*/instrument_id=*/part-*.parquet"))
    return parts[0] if parts else None


def _read(p: Path | None) -> pa.Table | None:
    """The table of a Parquet file an earlier edit may have deleted or damaged (None then: the edit is skipped)."""
    if p is None or not p.exists():
        return None
    try:
        return pq.read_table(p)
    except (OSError, pa.ArrowException):
        return None


def _rows(t: pa.Table | None) -> list[dict[str, Any]] | None:
    """The rows of a table, None when an earlier edit left a value Python cannot hold (a session date at the int32
    limit): the edit is skipped."""
    if t is None:
        return None
    try:
        return t.to_pylist()
    except (OverflowError, ValueError):
        return None


def _apply(root: Path, edits: list[tuple[str, Any]]) -> None:
    for kind, arg in edits:
        if kind == "instrument_values":
            p = root / "instruments.parquet"
            t = _read(p)
            rows = _rows(t)
            if t is not None and rows is not None:
                for r in rows:
                    for col, i in arg.items():
                        r[col] = INSTRUMENT_EDGES[col][i]
                pq.write_table(pa.Table.from_pylist(rows, schema=t.schema), p)
            continue
        if kind == "partition_types":
            p = _first_part(root)
            t = _read(p)
            col = _RETYPE_COLUMN[arg]
            if p is not None and t is not None and col in t.column_names:  # an earlier edit may have dropped it
                n = t.num_rows
                i = t.schema.get_field_index(col)
                if arg == "string_price":
                    t = t.set_column(i, col, pa.array(["x"] * n))
                elif arg == "null_size":
                    t = t.set_column(i, col, pa.array([None] * n, pa.uint64()))
                elif arg == "drop_action":
                    t = t.drop_columns([col])
                elif arg == "float_ts":
                    t = t.set_column(i, col, pa.array([1.5] * n))
                else:
                    t = t.set_column(i, col, pa.array([-1] * n, pa.int64()))
                pq.write_table(t, p)
            continue
        if kind == "instrument_price_exp":  # every instrument row and the manifest's fallback map
            t = _read(root / "instruments.parquet")
            if t is not None and _rows(t) is not None:
                rows = [{**r, "price_exp": arg} for r in _rows(t) or []]
                pq.write_table(pa.Table.from_pylist(rows, schema=t.schema), root / "instruments.parquet")
            continue
        if kind == "flip":
            name, flips = arg
            p = _first_part(root) if name == "partition" else root / name
            if p is not None and p.exists() and p.stat().st_size:
                data = bytearray(p.read_bytes())
                for at, byte in flips:
                    data[int(at * len(data))] = byte
                p.write_bytes(bytes(data))
            continue
        if kind == "manifest":
            p = root / "manifest.json"
            doc = json.loads(p.read_text())
            path, value = arg
            p.write_text(json.dumps(_set(doc, path, value), default=str))
            continue
        target = {
            "instruments": root / "instruments.parquet",
            "dq_json": root / "data_quality.json",
            "partition": _first_part(root),
        }.get(kind)
        if kind == "delete":
            target = _first_part(root) if arg == "partition" else root / arg
            if target is not None and target.exists():
                target.unlink()
            continue
        if target is None:
            continue
        if arg == "copy_manifest":
            target.write_bytes((root / "manifest.json").read_bytes())
        elif arg == "truncate" and target.exists():
            data = target.read_bytes()
            target.write_bytes(data[: len(data) // 2])
        elif arg == "json":
            target.write_text('{"source_specific": [1, 2], "verdict": {}}')
        elif isinstance(arg, bytes):
            target.write_bytes(arg)


def run_dq(ds: dict[str, Any], edits: list[tuple[str, Any]], *, replay: bool, what: str) -> Any:
    with workdir() as d:
        root = d / "ds"
        symbols = {1: "ACME", 2: "XYZ"}
        make_dataset(root, ds["parts"], symbols, venue=ds["venue"], tick=ds["tick"])
        _apply(root, edits)

        def run() -> Any:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                Dataset(root).describe()
                return dq.run({"dataset": {"path": str(root)}, "replay": replay, "out": str(d / "rep")})

        return guarded(what, run)


@FUZZ
@given(dataset(hostile=True))
def test_dq_hostile_records(ds: dict[str, Any]) -> None:
    run_dq(ds, [], replay=False, what="dq:records")


@FUZZ
@given(dataset(hostile=False), damage())
def test_dq_damaged_metadata(ds: dict[str, Any], edits: list[tuple[str, Any]]) -> None:
    run_dq(ds, edits, replay=False, what="dq:damaged")


@pytest.mark.requires_core
@scaled(0.5)
@given(dataset(hostile=True))
def test_dq_with_replay(ds: dict[str, Any]) -> None:
    """The book checks on the exact replay (Rust core) over random record streams."""
    run_dq(ds, [], replay=True, what="dq:replay")


@FUZZ
@given(
    dataset(hostile=True),
    st.dictionaries(st.sampled_from(sorted(INSTRUMENT_EDGES)), st.integers(0, 4), min_size=1),
    st.sampled_from(["nasdaq", "iex", "cme"]),
)
def test_dq_instrument_edges(ds: dict[str, Any], values: dict[str, int], venue: str) -> None:
    """Well-typed but extreme instrument rows (price_exp -128, tick 0, midnight at the int64 limits, session dates
    at the int32 limits, ...) reach every record check."""
    run_dq({**ds, "venue": venue}, [("instrument_values", values)], replay=False, what="dq:instrument_edges")


@FUZZ
@given(
    dataset(hostile=True),
    dataset(hostile=True),
    st.sampled_from([-12, -9, -4, -2, 0, -128, 127]),
    st.sampled_from([-12, -9, -4, 0, -13, 1]),
)
def test_dq_compare(a: dict[str, Any], b: dict[str, Any], exp_a: int, exp_b: int) -> None:
    """Cross-source parity of two random datasets whose `price_exp` differ (prices are rescaled to the finer one:
    the rescale must not wrap around int64), including exponents outside the dataset range -12..0."""
    with workdir() as d:
        roots = []
        for name, ds, exp in (("a", a, exp_a), ("b", b, exp_b)):
            root = make_dataset(d / name, ds["parts"], {1: "ACME", 2: "XYZ"}, venue="nasdaq", tick=ds["tick"])
            _apply(root, [("instrument_price_exp", exp)])
            roots.append(root)

        def run() -> Any:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                spec = {"dataset": {"path": str(roots[0])}, "compare": {"path": str(roots[1])}, "replay": False}
                return dq.run(spec)

        guarded("dq:compare", run)
