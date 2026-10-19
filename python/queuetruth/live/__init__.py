"""Canonical live order log, importers, schedule and latency statistics (SPEC 3.8, 3.9, 4.3 `queuetruth.live`, 5.7).

Every importer is fixture-validated only (`validation_status = fixture_only`). Importers never drop or invent data
silently: duplicates, unmappable records and messages without a canonical event are counted and reported in the
import summary's warnings (see `queuetruth.live.builder` for the shared rules). Logs built only from fill lists
carry a `coverage=fills_only` warning and are reported by `coverage()`: never-filled orders are absent from them,
so fill-rate bias and McNemar statistics are not meaningful on them.

Schema 1.1 (SPEC 3.11): import summaries and manifests carry `coverage {fills_only, n_fills_only_orders}` and
`entry_fallback_share` (the canonical value; one warning text `entry_fallback_share=<fb>/<n>: ...`, shared with
`to_schedule`, is added only when some order fell back); `LiveImportSpec.tz`,
`fix.venue_name` and the simulator venues `sim_hftbacktest` / `sim_nautilus` are read from the spec (keyword
arguments override). A spec field set to a value the chosen importer cannot honour fails `QT-CONFIG-001`.

Imports without an instrument map (or with instruments missing from it) are loud: a `LiveImportWarning` is
emitted and the summary's first warning, prefixed `unmapped_instruments:`, names every unmapped instrument (their
orders carry `instrument_id = null` and default scales, so they cannot be scheduled or scored). The per-instrument
scales of a log are in `instrument_scales(live_dir)`.

FIX engine logs: `import_quickfix` (or venue `fix44`) reads QuickFIX C++ / J / n / Go FileLog and FileStore files
and plain FIX logs (`queuetruth.live.importers.fix44`, `queuetruth.live.importers.quickfix`).
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
from pydantic import ValidationError

from queuetruth.errors import QtError
from queuetruth.live._common import valid_id
from queuetruth.live.builder import ImportResult
from queuetruth.live.canonical import (
    EVENT_COLUMNS,
    EVENT_COLUMNS_FULL,
    ORDER_COLUMNS,
    ORDER_COLUMNS_FULL,
    build_log,
    coverage_fields,
    entry_fallback,
    entry_fallback_warning,
    fills_only_keys,
    instrument_scales,
    manifest_sha256,
    read_log,
    scales_from_rows,
    table_rows,
    write_log,
)
from queuetruth.live.imap import InstrumentMap, load_instrument_map
from queuetruth.live.importers import SIM_IMPORTERS, VENUE_IMPORTERS, ImportOptions
from queuetruth.live.importers.quickfix import expand_inputs
from queuetruth.live.schedule import (
    ENTRY_KINDS,
    KEEP_PRIORITY_VENUES,
    RESTING_TIF,
    LiveScheduleWarning,
    Schedule,
    build_schedule,
    latency_stats,
    schedule_report,
    to_schedule,
)
from queuetruth.live.synthetic import synthetic_log
from queuetruth.live.validate import check, check_tables, raise_on_errors, validate
from queuetruth.schemas.io import sha256_file
from queuetruth.schemas.models import (
    FileEntry,
    InstrumentScale,
    LiveImportSummary,
    LiveInput,
    UnmappedInstrument,
)
from queuetruth.schemas.specs import FixOptions, LiveImportSpec

#: Venue name -> importer `(input paths, ImportOptions) -> ImportResult` (the nine `LiveImportSpec` venues).
IMPORTERS: dict[str, Callable[..., Any]] = dict(VENUE_IMPORTERS)
#: `LiveImportSpec.venue` values of the simulator importers (1.1, SPEC 3.11 #13) -> `import_sim` kind.
SIM_VENUES: dict[str, str] = {"sim_hftbacktest": "hftbacktest", "sim_nautilus": "nautilus"}
#: Venues whose importer reads `tz` (IBKR Flex account times; FIX log times captured by `fix.local_ts_regex`).
TZ_VENUES = frozenset({"ibkr_flex", "fix44"})


class LiveImportWarning(UserWarning):
    """Emitted by imports whose output cannot be scored as is (unmapped instruments)."""


def _paths(refs: list[Any]) -> list[str]:
    out = []
    for r in refs:
        p = getattr(r, "path", None) if not isinstance(r, dict) else r.get("path")
        if p is None:
            raise QtError("QT-CONFIG-001", "inputs: only local {path} references can be imported here")
        if not Path(p).exists():
            raise QtError("QT-INPUT-001", f"{p}: input not found")
        out.append(str(p))
    return out


def entry_fallback_note(orows: list[dict[str, Any]], erows: list[dict[str, Any]]) -> str | None:
    """The `entry_fallback_share=<fb>/<n>: ...` warning (`canonical.entry_fallback_warning`, the same text as
    `to_schedule`) when resting limit orders fall back to send_plus_latency (first ack without a match / gateway
    exchange time, SPEC 5.7.2); None when none does. The share is the summary's numeric `entry_fallback_share`."""
    return entry_fallback_warning(*entry_fallback(orows, erows))


def coverage_from_rows(orows: list[dict[str, Any]], erows: list[dict[str, Any]]) -> dict[str, Any]:
    """`coverage` on in-memory canonical rows."""
    fills_only = fills_only_keys(orows, erows)
    n = len(orows)
    return {
        "n_orders": n,
        "fills_only_orders": len(fills_only),
        "fills_only_share": None if n == 0 else len(fills_only) / n,
        "fills_only": n > 0 and len(fills_only) == n,
        "fills_only_keys": fills_only,
    }


def coverage(live_dir: str | Path) -> dict[str, Any]:
    """Order-source coverage of a live log: orders known only from fills (no order record or lifecycle event, no
    limit price) cannot show never-filled orders, so a scorer must not compute fill-rate bias or McNemar on them.
    Returns `{n_orders, fills_only_orders, fills_only_share, fills_only (all orders), fills_only_keys}`."""
    orders, events = read_log(live_dir)
    return coverage_from_rows(table_rows(orders), table_rows(events))


def write_canonical_log(
    out: str | Path,
    orders: list[dict[str, Any]] | pa.Table,
    events: list[dict[str, Any]] | pa.Table,
    *,
    validate_log: bool = True,
    instrument_map_sha256: str | None = None,
) -> list[FileEntry]:
    """Writes a canonical live-log directory (`orders.parquet`, `events.parquet`, `manifest.json`) from in-memory
    rows (dicts with canonical column names, see `build_log`) or canonical tables; validates first (errors raise
    `QtError`) unless `validate_log` is false."""
    if not isinstance(orders, pa.Table) or not isinstance(events, pa.Table):
        orders, events = build_log(
            orders if isinstance(orders, list) else table_rows(orders),
            events if isinstance(events, list) else table_rows(events),
        )
    if validate_log:
        errors, _, dup = check_tables(orders, events)
        raise_on_errors(errors, dup, str(out))
    files = write_log(out, orders, events, instrument_map_sha256=instrument_map_sha256)
    return [FileEntry(**f) for f in files]


def _merge_existing(out: Path, res: ImportResult) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Append mode: existing log rows + new rows; same-key orders keep the existing row, duplicate events
    (same fill id, or identical lifecycle event) are dropped and counted; events are renumbered."""
    old_o, old_e = read_log(out)
    orows, erows = table_rows(old_o), table_rows(old_e)
    keys = {o["order_key"] for o in orows}
    for o in res.orders:
        if o["order_key"] in keys:
            continue
        orows.append(o)
        keys.add(o["order_key"])
    seen: set[tuple[Any, ...]] = set()
    by_key: dict[str, list[dict[str, Any]]] = {}
    for e in erows:
        by_key.setdefault(e["order_key"], []).append(e)
    for e in sorted(erows, key=lambda r: (r["order_key"], r["event_seq"])):
        seen.add(_event_identity(e))
    dropped = 0
    for e in res.events:
        ident = _event_identity(e)
        if ident in seen:
            dropped += 1
            continue
        seen.add(ident)
        by_key.setdefault(e["order_key"], []).append(dict(e))
    merged: list[dict[str, Any]] = []
    for k in sorted(by_key):
        evs = by_key[k]
        evs.sort(key=lambda r: _best_ts(r))
        for i, e in enumerate(evs, start=1):
            e["event_seq"] = i
            merged.append(e)
    return orows, merged, dropped


def _event_identity(e: dict[str, Any]) -> tuple[Any, ...]:
    if e["event_type"] == "fill" and e.get("fill_id") is not None:
        return ("fill", e["order_key"], e["fill_id"])
    return (
        e["order_key"],
        e["event_type"],
        e.get("ts_local_send"),
        e.get("ts_exchange"),
        e.get("ts_local_recv"),
        e.get("qty"),
        e.get("px"),
    )


def _best_ts(e: dict[str, Any]) -> int:
    for k in ("ts_exchange", "ts_local_recv", "ts_local_send"):
        if e.get(k) is not None:
            return int(e[k])
    return 0


def unmapped_instruments(orows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`[{venue, instrument, n_orders}]` of instruments whose orders have no `instrument_id`, sorted (the 1.1
    `qt.live_import_summary.unmapped_instruments`)."""
    acc: dict[tuple[str, str], int] = {}
    for o in orows:
        if o["instrument_id"] is None:
            k = (o["venue"], o["instrument"])
            acc[k] = acc.get(k, 0) + 1
    return [{"venue": v, "instrument": i, "n_orders": n} for (v, i), n in sorted(acc.items())]


def unmapped_note(orows: list[dict[str, Any]]) -> str | None:
    """`unmapped_instruments: ...` warning for orders without an `instrument_id` (None when every order is mapped)."""
    unmapped: dict[tuple[str, str], int] = {}
    scales: set[tuple[int, int]] = set()
    for o in orows:
        if o["instrument_id"] is None:
            k = (o["venue"], o["instrument"])
            unmapped[k] = unmapped.get(k, 0) + 1
            scales.add((o["price_exp"], o["size_exp"]))
    if not unmapped:
        return None
    names = sorted(unmapped)
    shown = ", ".join(f"{v}/{i} ({unmapped[(v, i)]} orders)" for v, i in names[:50])
    more = f", ... ({len(names) - 50} more)" if len(names) > 50 else ""
    exps = "; ".join(f"price_exp {pe}, size_exp {se}" for pe, se in sorted(scales))
    return (
        f"unmapped_instruments: {len(names)} instrument(s) have no instrument-map entry: {shown}{more}. Their "
        f"{sum(unmapped.values())} order(s) carry instrument_id = null and default scales ({exps}) and cannot be "
        "scheduled or scored; pass an instrument map (LiveImportSpec.instrument_map / --instrument-map, columns "
        "venue,instrument,instrument_id,session_date,price_exp,size_exp,tick_size,lot_size)"
    )


def _finish_import(
    venue: str,
    res: ImportResult,
    paths: list[str],
    out: str | Path,
    *,
    append: bool,
    status: str = "fixture_only",
) -> LiveImportSummary:
    outp = Path(out)
    warnings_out = list(res.warnings)
    dedupe = res.dedupe_dropped
    if append and (outp / "orders.parquet").exists():
        orows, erows, dropped = _merge_existing(outp, res)
        dedupe += dropped
        if dropped:
            warnings_out.append(f"append: {dropped} event(s) already present in {out} dropped")
    else:
        orows, erows = res.orders, res.events
    orders, events = build_log(orows, erows)
    errors, val_warnings, dup = check_tables(orders, events)
    raise_on_errors(errors, dup, f"{venue} import")
    warnings_out.extend(val_warnings)
    o_rows, e_rows = table_rows(orders), table_rows(events)
    fb_note = entry_fallback_note(o_rows, e_rows)
    if fb_note is not None:
        warnings_out.append(fb_note)
    cov = coverage_from_rows(o_rows, e_rows)
    if cov["fills_only_orders"] and not any("coverage=fills_only" in w for w in warnings_out):
        warnings_out.append(f"coverage=fills_only for {cov['fills_only_orders']} of {cov['n_orders']} order(s)")
    unmapped = unmapped_note(o_rows)
    if unmapped is not None:
        warnings_out.insert(0, unmapped)
        warnings.warn(f"{venue} import to {out}: {unmapped}", LiveImportWarning, stacklevel=3)
    cov_doc, share = coverage_fields(o_rows, e_rows)
    unmapped_rows = unmapped_instruments(o_rows)
    scales = scales_from_rows(o_rows)
    files = write_log(outp, orders, events, instrument_map_sha256=res.instrument_map_sha256)
    return LiveImportSummary(
        venue=venue,
        inputs=[LiveInput(path=p, sha256=sha256_file(p)) for p in paths],
        n_orders=orders.num_rows,
        n_events=events.num_rows,
        n_fills=sum(1 for e in e_rows if e["event_type"] == "fill"),
        dedupe_dropped=dedupe,
        unmatched_fills=res.unmatched_fills,
        warnings=warnings_out,
        validation_status=status,  # type: ignore[arg-type]
        files=[FileEntry(**f) for f in files],
        coverage=cov_doc,
        entry_fallback_share=share,
        unmapped_instruments=[UnmappedInstrument(**u) for u in unmapped_rows],
        instrument_scales=[InstrumentScale(**x) for x in scales],
    )


def import_venue(spec: LiveImportSpec | dict[str, Any], *, tz: str | None = None) -> LiveImportSummary:
    """`live import`: venue exports -> canonical live-log directory `spec.out`.

    `tz` (IANA name) overrides `spec.tz`: the account time zone of IBKR Flex times, or the zone of `fix44` log times
    captured by `fix.local_ts_regex` without an offset; other venues reject it (`QT-CONFIG-001`). `fix` options
    apply to venue `fix44` only (`fix.venue_name` names the real exchange). Venues `sim_hftbacktest` /
    `sim_nautilus` run the simulator importers (`import_sim`). `fix44` inputs may be QuickFIX log / store
    directories; every `FixOptions` field (delimiter, local_ts_regex, venue_name, checksum, data_dictionary,
    session_venues, instrument_tag) is honoured. The summary carries the 1.1 `unmapped_instruments` and
    `instrument_scales` next to their warning lines."""
    s = spec if isinstance(spec, LiveImportSpec) else LiveImportSpec.model_validate(spec)
    if not s.out:
        raise QtError("QT-CONFIG-001", "out: an output directory is required")
    tz_eff = tz if tz is not None else s.tz
    fix_set = s.fix != FixOptions()
    if s.venue in SIM_VENUES:
        bad = [n for n, v in (("tz", tz_eff), ("fix", fix_set)) if v]
        if bad:
            raise QtError("QT-CONFIG-001", f"{', '.join(bad)}: not used by the {s.venue} simulator importer")
        return import_sim(
            SIM_VENUES[s.venue],
            _paths(list(s.inputs)),
            s.out,
            account=s.account,
            instrument_map=_paths([s.instrument_map])[0] if s.instrument_map is not None else None,
            append=s.append,
        )
    if fix_set and s.venue != "fix44":
        raise QtError("QT-CONFIG-001", f"fix: FIX options apply to venue fix44 only, not {s.venue}")
    if tz_eff and s.venue not in TZ_VENUES:
        raise QtError(
            "QT-CONFIG-001",
            f"tz: the {s.venue} importer reads UTC / epoch times only; tz applies to {sorted(TZ_VENUES)}",
        )
    paths = _paths(list(s.inputs))
    if s.venue == "fix44":
        paths = expand_inputs(paths)
    imap: InstrumentMap | None = None
    if s.instrument_map is not None:
        imap = load_instrument_map(_paths([s.instrument_map])[0])
    extra: dict[str, Any] = {"fix": s.fix.model_dump()}
    if tz_eff:
        extra["tz"] = tz_eff
    res = IMPORTERS[s.venue](paths, ImportOptions(account=s.account, imap=imap, extra=extra))
    return _finish_import(res.venue, res, paths, s.out, append=s.append)


def import_quickfix(
    inputs: list[str] | str,
    out: str | Path,
    *,
    fix: FixOptions | dict[str, Any] | None = None,
    venue_name: str | None = None,
    account: str | None = None,
    instrument_map: str | None = None,
    append: bool = False,
    data_dictionary: str | list[str] | None = None,
    checksum: str | None = None,
    session_venues: dict[str, str] | None = None,
    instrument_tag: int | None = None,
    delimiter: str | None = None,
    local_ts_regex: str | None = None,
    tz: str | None = None,
) -> LiveImportSummary:
    """QuickFIX (C++ / Python, J, n, Go) FileLog / FileStore files or directories, or plain FIX logs -> canonical
    live-log directory `out` (`queuetruth.live.importers.fix44` documents the semantics); the same import as
    `import_venue` with venue `fix44`.

    `fix` is a `LiveImportSpec.fix` (`FixOptions` or dict); every keyword given (not None) overrides its field:
    `venue_name` names the exchange (default `fix44`); `session_venues` maps a session (file stem such as
    `FIX.4.4-ME-CME`, or the counterparty CompID) to a venue; `data_dictionary` is one or more QuickFIX data
    dictionary XML files (custom tags and enums); `checksum` `exclude` (default) or `keep` for messages failing
    CheckSum / BodyLength; `instrument_tag` reads the instrument from another tag than Symbol(55) (e.g. 107
    SecurityDesc); `delimiter`, `local_ts_regex` as in `FixOptions`; `tz` as `LiveImportSpec.tz`. Invalid values
    fail `QT-CONFIG-001`."""
    base = fix.model_dump() if isinstance(fix, FixOptions) else dict(fix or {})
    given = {
        "delimiter": delimiter,
        "local_ts_regex": local_ts_regex,
        "venue_name": venue_name,
        "checksum": checksum,
        "data_dictionary": data_dictionary,
        "session_venues": session_venues,
        "instrument_tag": instrument_tag,
    }
    base.update({k: v for k, v in given.items() if v is not None})
    try:
        opts = FixOptions.model_validate(base)
    except ValidationError as e:
        raise QtError("QT-CONFIG-001", f"fix: {e.errors()[0]['loc']}: {e.errors()[0]['msg']}") from e
    paths = expand_inputs(_paths([{"path": p} for p in ([inputs] if isinstance(inputs, str) else inputs)]))
    imap = load_instrument_map(_paths([{"path": instrument_map}])[0]) if instrument_map else None
    extra: dict[str, Any] = {"fix": opts.model_dump()}
    if tz:
        extra["tz"] = tz
    res = IMPORTERS["fix44"](paths, ImportOptions(account=account, imap=imap, extra=extra))
    return _finish_import(res.venue, res, paths, out, append=append)


def import_sim(
    kind: str,
    inputs: list[str],
    out: str | Path,
    *,
    account: str | None = None,
    instrument_map: str | None = None,
    venue: str | None = None,
    append: bool = False,
    **options: Any,
) -> LiveImportSummary:
    """Simulator output (`kind` = `hftbacktest` or `nautilus`) -> canonical live-log directory for the scorer's
    external arm. `venue` names the live venue whose instrument-map rows apply; other options go to the importer
    (`order_key_map`, `key_from_client_id`, `size_exp`). The log is marked `venue = sim:<kind>`, `source = sim`,
    `validation_status = simulated` (schema 1.1; v0.1 used synthetic / fixture_only)."""
    if kind not in SIM_IMPORTERS:
        raise QtError("QT-CONFIG-001", f"simulator kind must be one of {sorted(SIM_IMPORTERS)}, got {kind!r}")
    paths = _paths([{"path": p} for p in inputs])
    imap = load_instrument_map(instrument_map) if instrument_map else None
    extra = dict(options)
    if venue:
        extra["venue"] = venue
    res = SIM_IMPORTERS[kind](paths, ImportOptions(account=account, imap=imap, extra=extra))
    return _finish_import(f"sim:{kind}", res, paths, out, append=append, status="simulated")


def manifest(path: str | Path) -> dict[str, Any]:
    """The `qt.live_manifest` document of a live-log directory."""
    p = Path(path) / "manifest.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise QtError("QT-INPUT-001", f"{p}: {e}") from e


__all__ = [
    "ENTRY_KINDS",
    "EVENT_COLUMNS",
    "EVENT_COLUMNS_FULL",
    "IMPORTERS",
    "KEEP_PRIORITY_VENUES",
    "ORDER_COLUMNS",
    "ORDER_COLUMNS_FULL",
    "RESTING_TIF",
    "SIM_IMPORTERS",
    "SIM_VENUES",
    "TZ_VENUES",
    "ImportOptions",
    "ImportResult",
    "InstrumentMap",
    "LiveImportWarning",
    "LiveScheduleWarning",
    "Schedule",
    "build_log",
    "build_schedule",
    "check",
    "check_tables",
    "coverage",
    "import_quickfix",
    "import_sim",
    "import_venue",
    "instrument_scales",
    "latency_stats",
    "load_instrument_map",
    "manifest",
    "manifest_sha256",
    "read_log",
    "schedule_report",
    "synthetic_log",
    "table_rows",
    "to_schedule",
    "unmapped_instruments",
    "valid_id",
    "validate",
    "write_canonical_log",
]
