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
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
from pydantic import ValidationError

from queuetruth import backtesters as _backtesters
from queuetruth.errors import QtError
from queuetruth.live import importers as _importers
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
from queuetruth.live.importers import SIM_IMPORTERS, VENUE_IMPORTERS, ImporterDef, ImportOptions
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

#: Venue name -> importer `(input paths, ImportOptions) -> ImportResult` (the venue importers of the registry,
#: `queuetruth.live.importers.registry()`; a read-only view).
IMPORTERS: Mapping[str, Callable[..., Any]] = VENUE_IMPORTERS
#: `LiveImportSpec.venue` values of the built-in simulator importers (1.1, SPEC 3.11 #13) -> `import_sim` kind
#: (plugins are reached through `import_sim` and `import_venue` too).
SIM_VENUES: dict[str, str] = {d.venue: d.name for d in _importers.builtins() if d.kind == "sim"}
#: Trading-bot venues of the live arm (`queuetruth.live.importers.BOT_IMPORTERS`): the log venue is the exchange.
BOT_VENUES = frozenset(d.venue for d in _importers.builtins() if d.kind == "bot")
#: Venues whose importer reads `tz` (IBKR Flex account times; FIX log times captured by `fix.local_ts_regex`).
TZ_VENUES = frozenset(d.venue for d in _importers.builtins() if d.kind == "venue" and "tz" in d.options.option_names())


class _AnyVenueSpec(LiveImportSpec):
    """`LiveImportSpec` for a venue registered by a plugin (the spec's enum lists the built-ins)."""

    venue: str  # type: ignore[assignment]


def live_import_spec(spec: LiveImportSpec | dict[str, Any]) -> LiveImportSpec:
    """`spec` as a `LiveImportSpec` (pydantic validation, as `LiveImportSpec.model_validate`); a venue registered by a
    plugin (`queuetruth.importers` entry point) is accepted too."""
    if isinstance(spec, LiveImportSpec):
        return spec
    venue = spec.get("venue") if isinstance(spec, dict) else None
    plugin = (
        isinstance(venue, str) and venue not in _importers.live_venues(builtin=True) and venue in _importers.registry()
    )
    return (_AnyVenueSpec if plugin else LiveImportSpec).model_validate(spec)


def _only_for(option: str) -> str:
    venues = [d.venue for d in _importers.importers("venue") if option in d.options.option_names()]
    return f"venue {', '.join(venues)}" if len(venues) == 1 else f"venues {', '.join(venues)}"


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


def import_venue(spec: LiveImportSpec | dict[str, Any], *, tz: str | None = None, **options: Any) -> LiveImportSummary:
    """`live import`: venue exports -> canonical live-log directory `spec.out`.

    `tz` (IANA name) overrides `spec.tz`: the account time zone of IBKR Flex times, or the zone of `fix44` log times
    captured by `fix.local_ts_regex` without an offset; other venues reject it (`QT-CONFIG-001`). `fix` options
    apply to venue `fix44` only (`fix.venue_name` names the real exchange). Venues `sim_hftbacktest` /
    `sim_nautilus` run the simulator importers (`import_sim`). `fix44` inputs may be QuickFIX log / store
    directories; every `FixOptions` field (delimiter, local_ts_regex, venue_name, checksum, data_dictionary,
    session_venues, instrument_tag) is honoured. The summary carries the 1.1 `unmapped_instruments` and
    `instrument_scales` next to their warning lines.

    The importer is the registry's (`queuetruth.live.importers.by_venue`), plugins included. `options` are the
    importer's own (its typed options model, e.g. `flex_date_order` for `ibkr_flex`); a wrong or unknown one fails
    `QT-CONFIG-001`."""
    s = live_import_spec(spec)
    if not s.out:
        raise QtError("QT-CONFIG-001", "out: an output directory is required")
    d = _importers.by_venue(s.venue)
    tz_eff = tz if tz is not None else s.tz
    fix_set = s.fix != FixOptions()
    if d.kind == "sim":
        bad = [n for n, v in (("tz", tz_eff), ("fix", fix_set)) if v]
        if bad:
            raise QtError("QT-CONFIG-001", f"{', '.join(bad)}: not used by the {s.venue} simulator importer")
        return import_sim(
            d.name,
            _paths(list(s.inputs)),
            s.out,
            account=s.account,
            instrument_map=_paths([s.instrument_map])[0] if s.instrument_map is not None else None,
            append=s.append,
            **options,
        )
    if d.kind == "bot":
        return import_bot(s, tz=tz_eff, **options)
    known = d.options.option_names()
    if fix_set and "fix" not in known:
        raise QtError("QT-CONFIG-001", f"fix: FIX options apply to {_only_for('fix')} only, not {s.venue}")
    if tz_eff and "tz" not in known:
        raise QtError(
            "QT-CONFIG-001",
            f"tz: the {s.venue} importer reads UTC / epoch times only; tz applies to {sorted(TZ_VENUES)}",
        )
    extra: dict[str, Any] = dict(options)
    if "fix" in known:
        extra["fix"] = s.fix.model_dump()
    if tz_eff:
        extra["tz"] = tz_eff
    d.validate(s.account, None, **extra)  # before any input is read
    paths = _paths(list(s.inputs))
    if "fix" in known:
        paths = expand_inputs(paths)
    imap: InstrumentMap | None = None
    if s.instrument_map is not None:
        imap = load_instrument_map(_paths([s.instrument_map])[0])
    res = d.run(paths, d.import_options(s.account, imap, **extra))
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
    d = _importers.by_venue("fix44")
    res = d.run(paths, d.import_options(account, imap, **extra))
    return _finish_import(res.venue, res, paths, out, append=append)


def import_bot(spec: LiveImportSpec, *, tz: str | None = None, **options: Any) -> LiveImportSummary:
    """A trading bot's live (or dry-run) run -> canonical live-log directory `spec.out` (`freqtrade`: trades database
    and bot logs; `hummingbot`: database or trades CSV). The log venue is the exchange the bot traded on; a dry-run
    or paper run is marked `validation_status = simulated`. `options` are the importer's (`dry_run`, `venue`, `tif`,
    `log_tz`, `timeframe`, `price_exp`, `size_exp`, ...; docs/how-to/reconcile-freqtrade-hummingbot.md); bot log
    times take `log_tz`, not `tz`."""
    d = _importers.registry().get(spec.venue)
    if d is None or d.kind != "bot":
        bots = sorted(x.venue for x in _importers.importers("bot"))
        raise QtError("QT-CONFIG-001", f"venue {spec.venue!r} is not a trading bot ({bots})")
    if not spec.out:
        raise QtError("QT-CONFIG-001", "out: an output directory is required")
    if tz:
        raise QtError("QT-CONFIG-001", f"tz: bot log times take the importer option log_tz (e.g. log_tz={tz})")
    if spec.fix != FixOptions():
        raise QtError("QT-CONFIG-001", f"fix: FIX options apply to {_only_for('fix')} only, not {spec.venue}")
    d.validate(spec.account, None, **options)
    bt = _backtesters.registry().get(spec.venue)
    run = getattr(bt, "import_live", None)
    if run is None:  # a bot importer without its own writer (a plugin): import, then write the log
        return _import_with(d, spec, options)
    return run(
        _paths(list(spec.inputs)),
        spec.out,
        account=spec.account,
        instrument_map=_paths([spec.instrument_map])[0] if spec.instrument_map is not None else None,
        append=spec.append,
        **options,
    )


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
    """Simulator output (`kind` = `hftbacktest`, `nautilus`, `freqtrade`, `hummingbot`, `backtrader`, `vectorbt` or
    `lean`) -> canonical live-log directory for the scorer's external arm. `venue` names the live venue whose
    instrument-map rows apply; other options go to the importer (`order_key_map`, `key_from_client_id`, `size_exp`;
    for `backtrader`, `vectorbt` and `lean` `pair_with` = a canonical live-log directory, `timeframe`, ...). The
    log is marked `venue = sim:<kind>`, `source = sim`, `validation_status = simulated` (schema 1.1; 0.1.0 used
    synthetic / fixture_only)."""
    sims = {d.name: d for d in _importers.importers("sim")}
    if kind not in _backtesters.registry() and kind not in sims:
        known = sorted(set(_backtesters.registry()) | set(sims))
        raise QtError("QT-CONFIG-001", f"simulator kind must be one of {known}, got {kind!r}")
    if kind == "lean":
        from queuetruth.live.importers.lean import check_append

        check_append(out, append)
    extra = dict(options)
    if venue:
        extra["venue"] = venue
    d = sims.get(kind)
    if d is not None:
        d.validate(account, None, **extra)  # before any input is read
    paths = _paths([{"path": p} for p in inputs])
    imap = load_instrument_map(instrument_map) if instrument_map else None
    # the registry's typed options when it has the importer; a backtester plugin without one checks its own
    opts = d.import_options(account, imap, **extra) if d is not None else ImportOptions(account, imap, extra)
    bt = _backtesters.registry().get(kind)
    run = bt.import_sim if bt is not None else d.run  # type: ignore[union-attr]
    res = run(paths, opts)
    return _finish_import(f"sim:{kind}", res, paths, out, append=append, status="simulated")


def _import_with(d: ImporterDef, spec: LiveImportSpec, options: dict[str, Any]) -> LiveImportSummary:
    """Runs the importer `d` on the spec's inputs and writes the canonical log `spec.out`."""
    paths = _paths(list(spec.inputs))
    imap = load_instrument_map(_paths([spec.instrument_map])[0]) if spec.instrument_map is not None else None
    res = d.run(paths, d.import_options(spec.account, imap, **options))
    assert spec.out is not None
    return _finish_import(res.venue, res, paths, spec.out, append=spec.append)


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
    "live_import_spec",
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
