"""`live import` and `live validate`."""

from __future__ import annotations

from typing import Any

from queuetruth.errors import QtError
from queuetruth.ops._call import _path_of, _validate
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as s


def import_live(
    spec: s.LiveImportSpec | dict[str, Any], *, tz: str | None = None, **options: Any
) -> m.LiveImportSummary:
    """`live import`: venue exports (`queuetruth.live.import_venue`), simulator reports (venues `sim_hftbacktest`,
    `sim_nautilus`, `sim_freqtrade`, `sim_hummingbot`, `sim_backtrader`, `sim_vectorbt`, `sim_lean`:
    `queuetruth.live.import_sim`, log venue `sim:<name>`, for `score --sim-log`) or a trading bot's live run
    (`freqtrade`, `hummingbot`: `queuetruth.live.import_bot`). `tz` (default: the spec's `tz`) is the account time
    zone of venue-local times (IBKR Flex). `options` go to the simulator or bot importer (`venue` = live venue of the
    instrument-map rows, `size_exp`, `key_from_client_id`, `order_key_map`; for bots `dry_run`, `log_tz`,
    `timeframe`, `pair_with`, ...; for backtesters `pair_with` = a canonical live-log directory, `timeframe`,
    `instrument`, ...); for a venue whose importer has options of its own (`ibkr_flex`: `flex_date_order`), those.
    Each importer's options are a typed model (`queuetruth.live.importers.by_venue(venue).options`); a wrong or
    unknown one fails `QT-CONFIG-001`. A venue registered by a plugin (`queuetruth.importers`) works the same."""
    from queuetruth import live as qlive

    sp = qlive.live_import_spec(spec) if _plugin_venue(spec) else _validate(s.LiveImportSpec, spec)
    tz = tz if tz is not None else sp.tz
    kind = qlive.importers.by_venue(sp.venue).kind
    if kind == "bot":
        return qlive.import_bot(sp, tz=tz, **options)
    if kind == "sim":
        if not sp.out:
            raise QtError("QT-CONFIG-001", "out: an output directory is required")
        if tz is not None:
            raise QtError("QT-CONFIG-001", "tz: simulator reports carry UTC epoch timestamps; tz does not apply")
        inputs = [_path_of(r, "inputs") for r in sp.inputs]
        imap = _path_of(sp.instrument_map, "instrument_map") if sp.instrument_map is not None else None
        return qlive.import_sim(
            qlive.importers.by_venue(sp.venue).name,
            inputs,
            sp.out,
            account=sp.account,
            instrument_map=imap,
            append=sp.append,
            **options,
        )
    return qlive.import_venue(sp, tz=tz, **options)


def _plugin_venue(spec: Any) -> bool:
    """Whether a spec dict names a venue that only a plugin importer registers (not in `LiveImportSpec.venue`)."""
    from queuetruth.live import importers

    venue = spec.get("venue") if isinstance(spec, dict) else None
    return isinstance(venue, str) and venue not in importers.live_venues(builtin=True) and venue in importers.registry()


def validate_live(path: str) -> list[str]:
    """`live validate`: warnings; raises `QtError` on errors."""
    from queuetruth import live as qlive

    return list(qlive.validate(path))
