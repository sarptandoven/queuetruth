"""Canonical live order log (SPEC 3.8): `qt.live_orders` + `qt.live_events` table builders, the directory layout
`<live>/{orders.parquet, events.parquet, manifest.json}` and readers (Parquet, or the same columns as CSV).

Row order is normative for determinism: orders sorted by `order_key`, events by (`order_key`, `event_seq`).
Dictionary columns are encoded against the fixed value list of `queuetruth.schemas.arrow.DICT_VALUES`.

Schema 1.1 (SPEC 3.11 #14, #16): `build_log` and `write_log` produce the full 1.1 layout (`qt.live_orders` gains
`replaces_order_key`; `qt.live_events` is unchanged); readers accept both the v0.1 and the 1.1 layout
(`queuetruth.schemas.arrow.conform`) and return the 1.1 layout (`upgrade`: missing 1.1 columns are null). The
manifest carries `coverage` and `entry_fallback_share`.

Instrument scales: `orders.parquet` carries the key-value metadata `qt.live.instrument_scales`, a JSON array of
`{venue, instrument, instrument_id, price_exp, size_exp, scale_source, n_orders}` (one entry per distinct scale
of an instrument; `scale_source` = `instrument_map` when the instrument map gave the row, else `default`, the
SPEC 3.9 defaults). `instrument_scales(live_dir)` returns it (derived from the order rows for logs without it), so
a consumer can compare the log's units with a dataset's and normalise or refuse a mismatch.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from queuetruth._version import __version__
from queuetruth.errors import QtError
from queuetruth.live._common import input_error, read_text
from queuetruth.schemas.arrow import DICT_VALUES, FULL_SCHEMAS, SCHEMAS, conform, upgrade
from queuetruth.schemas.io import sha256_file, write_table
from queuetruth.schemas.models import LiveCoverage, LiveManifest

ORDERS = "qt.live_orders"
EVENTS = "qt.live_events"
#: v0.1 columns (every 1.1 file starts with them).
ORDER_COLUMNS: list[str] = [f.name for f in SCHEMAS[ORDERS]]
EVENT_COLUMNS: list[str] = [f.name for f in SCHEMAS[EVENTS]]
#: Full 1.1 columns (what `build_log` / `write_log` produce and readers return).
ORDER_COLUMNS_FULL: list[str] = [f.name for f in FULL_SCHEMAS[ORDERS]]
EVENT_COLUMNS_FULL: list[str] = [f.name for f in FULL_SCHEMAS[EVENTS]]
SCALES_KEY = b"qt.live.instrument_scales"
ORDERS_FILE = "orders.parquet"
EVENTS_FILE = "events.parquet"
MANIFEST_FILE = "manifest.json"
CREATED_BY = f"queuetruth {__version__}"

#: Defaults for omitted columns in `build_log` rows (other nullable columns default to null).
ORDER_DEFAULTS: dict[str, Any] = {"post_only": False, "validation_status": "fixture_only"}
EVENT_DEFAULTS: dict[str, Any] = {"derived": False}


def _column(values: list[Any], f: pa.Field, table: str) -> pa.Array:
    t = f.type
    if pa.types.is_dictionary(t):
        allowed = DICT_VALUES[table][f.name]
        index = {v: i for i, v in enumerate(allowed)}
        idx: list[int | None] = []
        for v in values:
            if v is None:
                idx.append(None)
            elif v in index:
                idx.append(index[v])
            else:
                raise input_error(table, f"{f.name}: value {v!r} not in {list(allowed)}")
        return pa.DictionaryArray.from_arrays(pa.array(idx, type=pa.int8()), pa.array(list(allowed), type=pa.utf8()))
    try:
        return pa.array(values, type=t)
    except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError, TypeError) as e:
        raise input_error(table, f"{f.name}: {e}") from e


def _table(rows: list[dict[str, Any]], name: str, defaults: dict[str, Any]) -> pa.Table:
    schema = FULL_SCHEMAS[name]
    cols = [f.name for f in schema]
    known = set(cols)
    for i, r in enumerate(rows):
        extra = set(r) - known
        if extra:
            raise input_error(f"{name} row {i}", f"unknown columns {sorted(extra)}")
    arrays = []
    for f in schema:
        vals = [r.get(f.name, defaults.get(f.name)) for r in rows]
        if not f.nullable:
            for i, v in enumerate(vals):
                if v is None:
                    raise input_error(f"{name} row {i}", f"{f.name} must not be null")
        arrays.append(_column(vals, f, name))
    return pa.Table.from_arrays(arrays, schema=schema)


def build_log(orders: list[dict[str, Any]], events: list[dict[str, Any]]) -> tuple[pa.Table, pa.Table]:
    """Canonical `qt.live_orders` and `qt.live_events` tables from row dicts with canonical column names.

    Omitted nullable columns are null (`post_only` defaults to false, `validation_status` to `fixture_only`,
    `derived` to false; the 1.1 column `replaces_order_key` may be omitted). The tables have the full 1.1 layout.
    Events without `event_seq` are numbered 1.. per `order_key` in the given order. Rows are
    sorted (orders by `order_key`, events by (`order_key`, `event_seq`)); the result is not validated (see
    `queuetruth.live.validate`).
    """
    orders = sorted((dict(o) for o in orders), key=lambda r: str(r.get("order_key")))
    evs = [dict(e) for e in events]
    seq: dict[str, int] = {}
    for e in evs:
        k = str(e.get("order_key"))
        if e.get("event_seq") is None:
            seq[k] = seq.get(k, 0) + 1
            e["event_seq"] = seq[k]
        else:
            seq[k] = max(seq.get(k, 0), int(e["event_seq"]))
    evs.sort(key=lambda r: (str(r.get("order_key")), int(r["event_seq"])))
    return _table(orders, ORDERS, ORDER_DEFAULTS), _table(evs, EVENTS, EVENT_DEFAULTS)


def table_rows(table: pa.Table) -> list[dict[str, Any]]:
    """Rows of a table as dicts; timestamps as integer ns, dictionary values as strings."""
    cols: dict[str, list[Any]] = {}
    for f in table.schema:
        col = table.column(f.name)
        if pa.types.is_timestamp(f.type):
            col = col.cast(pa.int64())
        elif pa.types.is_dictionary(f.type):
            col = col.cast(f.type.value_type)
        cols[f.name] = col.to_pylist()
    n = table.num_rows
    return [{k: v[i] for k, v in cols.items()} for i in range(n)]


RESTING_TIF = ("gtc", "day", "post_only", "gtx", "gtd")
ENTRY_KINDS = ("match_time", "gateway_time")
_FILL_TYPES = ("fill", "fill_bust", "fill_correct")


def fills_only_keys(orows: list[dict[str, Any]], erows: list[dict[str, Any]]) -> list[str]:
    """Order keys known only from fills (no order record or lifecycle event, no limit price), sorted."""
    kinds: dict[str, set[str]] = {}
    for e in erows:
        kinds.setdefault(e["order_key"], set()).add(e["event_type"])
    return sorted(
        o["order_key"] for o in orows if o["limit_px"] is None and kinds.get(o["order_key"], set()) <= set(_FILL_TYPES)
    )


def entry_fallback(orows: list[dict[str, Any]], erows: list[dict[str, Any]]) -> tuple[int, int]:
    """(fallbacks, schedulable orders) with the schedule's own filter (SPEC 5.7.2, `schedule_from_rows`) except the
    instrument mapping: resting limit orders with a limit price, not rejected before an ack, with an entry time.
    A fallback is such an order whose first ack has no match / gateway exchange time, so `entry = exchange_ack`
    uses its `submit_sent` local time (send_plus_latency)."""
    per: dict[str, list[dict[str, Any]]] = {}
    for e in erows:
        per.setdefault(e["order_key"], []).append(e)
    fb = n = 0
    for o in orows:
        if o["order_type"] != "limit" or o["tif"] not in RESTING_TIF or o["limit_px"] is None:
            continue
        evs = sorted(per.get(o["order_key"], []), key=lambda r: r["event_seq"])
        types = [e["event_type"] for e in evs]
        if "ack" not in types and any(t in ("reject", "post_only_reject") for t in types):
            continue
        a = next((e for e in evs if e["event_type"] == "ack"), None)
        if a is not None and a["ts_exchange"] is not None and a["ts_exchange_kind"] in ENTRY_KINDS:
            n += 1
            continue
        sub = next((e for e in evs if e["event_type"] == "submit_sent"), None)
        if sub is None or sub["ts_local_send"] is None:
            continue  # no entry time: excluded by the schedule
        n += 1
        fb += 1
    return fb, n


def entry_fallback_warning(fb: int, n: int) -> str | None:
    """The one `entry_fallback_share` warning text (import summaries and `to_schedule` alike; review R-B5: none when
    no order fell back). The value itself is the 1.1 numeric field `entry_fallback_share` (`fb / n`, null when
    `n == 0`) of the import summary, the live manifest and `schedule_report`."""
    if fb <= 0 or n <= 0:
        return None
    return f"entry_fallback_share={fb}/{n}: orders without a match/gateway-time ack scheduled with send_plus_latency"


def coverage_fields(
    orders: pa.Table | list[dict[str, Any]], events: pa.Table | list[dict[str, Any]]
) -> tuple[LiveCoverage, float | None]:
    """1.1 (3.11 #14) `coverage` and `entry_fallback_share` of canonical tables or their rows (share null without
    schedulable orders)."""
    orows = table_rows(orders) if isinstance(orders, pa.Table) else orders
    erows = table_rows(events) if isinstance(events, pa.Table) else events
    fo = fills_only_keys(orows, erows)
    cov = LiveCoverage(fills_only=bool(orows) and len(fo) == len(orows), n_fills_only_orders=len(fo))
    fb, n = entry_fallback(orows, erows)
    return cov, (None if n == 0 else fb / n)


def write_manifest(
    out: Path, orders: pa.Table, events: pa.Table, *, instrument_map_sha256: str | None = None
) -> dict[str, Any]:
    """Writes `manifest.json` (`qt.live_manifest`, with the 1.1 `coverage` and `entry_fallback_share`) and returns
    its `Files` entry."""
    venues = sorted(set(orders.column("venue").to_pylist()))
    accounts = sorted(set(orders.column("account").to_pylist()))
    cov, share = coverage_fields(orders, events)
    doc = LiveManifest(
        venues=venues,
        accounts=accounts,
        n_orders=orders.num_rows,
        n_events=events.num_rows,
        instrument_map_sha256=instrument_map_sha256,
        created_by=CREATED_BY,
        coverage=cov,
        entry_fallback_share=share,
    )
    p = out / MANIFEST_FILE
    p.write_text(json.dumps(doc.model_dump(), indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return {"name": MANIFEST_FILE, "rows": None, "bytes": p.stat().st_size, "sha256": sha256_file(p)}


def scales_from_rows(orows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Instrument-scale entries (see the module docstring) of canonical order rows, sorted."""
    acc: dict[tuple[Any, ...], int] = {}
    for o in orows:
        k = (o["venue"], o["instrument"], o["instrument_id"], o["price_exp"], o["size_exp"])
        acc[k] = acc.get(k, 0) + 1
    out = []
    for (venue, inst, iid, pexp, sexp), n in sorted(acc.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        out.append(
            {
                "venue": venue,
                "instrument": inst,
                "instrument_id": iid,
                "price_exp": pexp,
                "size_exp": sexp,
                "scale_source": "instrument_map" if iid is not None else "default",
                "n_orders": n,
            }
        )
    return out


def instrument_scales(path: str | Path) -> list[dict[str, Any]]:
    """Per-instrument price / size exponents of a canonical live-log directory: the `qt.live.instrument_scales`
    metadata of `orders.parquet`, else derived from the order rows (CSV logs, logs written before it existed).
    An instrument with more than one entry changed scale within the log (date-dependent map rows)."""
    p = Path(path) / ORDERS_FILE
    if p.is_file():
        try:
            meta = pq.read_schema(p).metadata or {}
        except (OSError, pa.ArrowInvalid) as e:
            raise QtError("QT-INPUT-001", f"{p}: {e}") from e
        if SCALES_KEY in meta:
            try:
                doc = json.loads(meta[SCALES_KEY].decode())
            except (ValueError, UnicodeDecodeError) as e:
                raise input_error(str(p), f"{SCALES_KEY.decode()}: {e}") from e
            if isinstance(doc, list):
                return doc
    orders, _ = read_log(path)
    return scales_from_rows(table_rows(orders))


def write_log(
    out: str | Path, orders: pa.Table, events: pa.Table, *, instrument_map_sha256: str | None = None
) -> list[dict[str, Any]]:
    """Writes a canonical live-log directory in the 1.1 layout (v0.1 tables are upgraded; `orders.parquet` carries
    the instrument-scale metadata); returns the `Files` entries (orders, events, manifest)."""
    p = Path(out)
    p.mkdir(parents=True, exist_ok=True)
    orders, events = upgrade(orders, ORDERS), upgrade(events, EVENTS)
    scales = json.dumps(scales_from_rows(table_rows(orders)), separators=(",", ":"), sort_keys=True)
    order_schema = FULL_SCHEMAS[ORDERS].with_metadata({**(FULL_SCHEMAS[ORDERS].metadata or {}), SCALES_KEY: scales})
    files = [
        write_table(orders, p / ORDERS_FILE, order_schema),
        write_table(events, p / EVENTS_FILE, FULL_SCHEMAS[EVENTS]),
    ]
    files.append(write_manifest(p, orders, events, instrument_map_sha256=instrument_map_sha256))
    return files


def _read_csv_table(path: Path, name: str) -> pa.Table:
    text = read_text(path)
    reader = csv.DictReader(io.StringIO(text))
    schema = FULL_SCHEMAS[name]
    rows: list[dict[str, Any]] = []
    for n, raw in enumerate(reader, start=2):
        row: dict[str, Any] = {}
        for f in schema:
            v = raw.get(f.name)
            if v is None or v == "":
                row[f.name] = None
                continue
            t = f.type
            try:
                if pa.types.is_boolean(t):
                    low = v.strip().lower()
                    if low not in ("true", "false", "1", "0"):
                        raise ValueError(f"not a boolean: {v!r}")
                    row[f.name] = low in ("true", "1")
                elif pa.types.is_integer(t) or pa.types.is_timestamp(t):
                    row[f.name] = int(v)
                elif pa.types.is_floating(t):
                    row[f.name] = float(v)
                elif pa.types.is_list(t):
                    parsed = json.loads(v)
                    if not isinstance(parsed, list):
                        raise ValueError("list column must hold a JSON array")
                    row[f.name] = [str(x) for x in parsed]
                else:
                    row[f.name] = v
            except (ValueError, json.JSONDecodeError) as e:
                raise input_error(f"{path}:{n}", f"{f.name}: {e}") from e
        rows.append(row)
    defaults = ORDER_DEFAULTS if name == ORDERS else EVENT_DEFAULTS
    return _table(rows, name, defaults)


def _read_parquet(path: Path, name: str) -> pa.Table:
    try:
        t = pq.read_table(path)
    except (OSError, pa.ArrowInvalid) as e:
        raise QtError("QT-INPUT-001", f"{path}: {e}") from e
    meta = t.schema.metadata or {}
    if meta.get(b"qt.schema", b"").decode() != name:
        raise input_error(str(path), f"expected qt.schema = {name}, got {meta.get(b'qt.schema')!r}")
    try:
        return upgrade(conform(t, name), name)
    except ValueError as e:
        raise input_error(str(path), f"columns differ from {name} (v0.1 or 1.1 layout): {e}") from e
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as e:
        raise input_error(str(path), f"types differ from {name}: {e}") from e


def read_log(path: str | Path) -> tuple[pa.Table, pa.Table]:
    """(`orders`, `events`) of a canonical live-log directory: `orders.parquet` + `events.parquet` (v0.1 or 1.1
    layout), or the same columns as `orders.csv` + `events.csv` (timestamps as integer ns, lists as JSON arrays;
    1.1 columns optional). Tables are returned in the full 1.1 layout."""
    p = Path(path)
    if not p.is_dir():
        raise QtError("QT-INPUT-001", f"{p}: live-log directory not found")
    pairs = [(p / ORDERS_FILE, p / EVENTS_FILE, _read_parquet), (p / "orders.csv", p / "events.csv", _read_csv_table)]
    for o, e, reader in pairs:
        if o.exists() and e.exists():
            return reader(o, ORDERS), reader(e, EVENTS)
    raise QtError("QT-INPUT-001", f"{p}: no orders.parquet/events.parquet (or orders.csv/events.csv)")


def manifest_sha256(path: str | Path) -> str | None:
    """sha256 of `<live>/manifest.json` (the directory's identity for `run_id`), or None when absent."""
    m = Path(path) / MANIFEST_FILE
    return sha256_file(m) if m.exists() else None
