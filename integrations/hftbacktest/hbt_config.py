"""hftbacktest queue-model configuration exported from queuetruth calibrations (`qtint.hbt_queue_config` v1).

This module is standard library only so that it runs in both environments: the queuetruth venv, where
`export_config.py` writes the file, and a separate hftbacktest venv, where `apply` turns one entry into
`BacktestAsset` builder calls. It never imports queuetruth or hftbacktest at module level.

File layout (`hbt_queue_config.json`, canonical JSON: sorted keys, two-space indent, trailing newline):

    {
      "schema": "qtint.hbt_queue_config", "schema_version": 1,
      "queuetruth_version": "0.1.1",
      "models": [
        {"model_id": "hbt_power3[exch=partial;mode=faithful;n=3.097579196829366]",
         "family": "power3", "builder": "power_prob_queue_model3", "args": [3.097579196829366],
         "exchange": "partial", "exchange_builder": "partial_fill_exchange",
         "role": "fitted" | "default" | "fixed",
         "train_quasi_loglik": float | null, "train_events": int | null,
         "source": {...provenance of the calibration...},
         "notes": [str, ...]},
        ...
      ]
    }

`model_id` follows the queuetruth ModelId grammar (spec 5.3.2) with the parameter printed at full precision, so
the same string can be passed to `queuetruth zoo --models` and selects the identical model.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

SCHEMA = "qtint.hbt_queue_config"
SCHEMA_VERSION = 1

# family -> (hftbacktest BacktestAsset builder, number of float arguments)
BUILDERS: dict[str, tuple[str, int]] = {
    "power": ("power_prob_queue_model", 1),
    "power2": ("power_prob_queue_model2", 1),
    "power3": ("power_prob_queue_model3", 1),
    "log": ("log_prob_queue_model", 0),
    "log2": ("log_prob_queue_model2", 0),
    "risk_adverse": ("risk_adverse_queue_model", 0),
}
EXCHANGE_BUILDERS: dict[str, str] = {"partial": "partial_fill_exchange", "no_partial": "no_partial_fill_exchange"}
ROLES = ("fitted", "default", "fixed")


class ConfigError(ValueError):
    """An invalid `qtint.hbt_queue_config` document or entry (message starts with `QT-CONFIG-001`)."""


def _err(msg: str) -> ConfigError:
    return ConfigError(f"QT-CONFIG-001: {msg}")


def fmt_param(x: float) -> str:
    """Shortest round-trip positional spelling, as Rust `{}` prints an f64 (`2` not `2.0`, no exponent).

    Equivalent to `numpy.format_float_positional(x, unique=True, trim="-")` for finite values; stdlib only.
    """
    if not math.isfinite(x):
        raise _err(f"parameter {x!r} is not finite")
    s = format(Decimal(repr(float(x))), "f")
    if s.endswith(".0"):
        s = s[:-2]
    return s


def model_id(family: str, exchange: str, args: Iterable[float], mode: str = "faithful") -> str:
    """queuetruth ModelId of an hbt queue model (keys sorted; `n` only for the power families)."""
    keys = {"exch": exchange, "mode": mode}
    a = list(args)
    if a:
        keys["n"] = fmt_param(a[0])
    return f"hbt_{family}[" + ";".join(f"{k}={v}" for k, v in sorted(keys.items())) + "]"


def make_entry(
    family: str,
    exchange: str,
    args: Iterable[float],
    role: str,
    *,
    train_quasi_loglik: float | None = None,
    train_events: int | None = None,
    source: Mapping[str, Any] | None = None,
    notes: Iterable[str] = (),
) -> dict[str, Any]:
    """One validated `models[]` entry."""
    a = [float(x) for x in args]
    entry: dict[str, Any] = {
        "model_id": "",
        "family": family,
        "builder": BUILDERS[family][0] if family in BUILDERS else "",
        "args": a,
        "exchange": exchange,
        "exchange_builder": EXCHANGE_BUILDERS.get(exchange, ""),
        "role": role,
        "train_quasi_loglik": None if train_quasi_loglik is None else float(train_quasi_loglik),
        "train_events": None if train_events is None else int(train_events),
        "source": dict(source or {}),
        "notes": list(notes),
    }
    validate_entry(entry, check_id=False)
    entry["model_id"] = model_id(family, exchange, a)
    return entry


def validate_entry(entry: Mapping[str, Any], check_id: bool = True) -> None:
    """Raise `ConfigError` unless `entry` is a well-formed models[] entry."""
    fam = entry.get("family")
    if fam not in BUILDERS:
        raise _err(f"family {fam!r} has no hftbacktest queue model (expected one of {sorted(BUILDERS)})")
    builder, n_args = BUILDERS[fam]
    if entry.get("builder") != builder:
        raise _err(f"family {fam} uses builder {builder}, not {entry.get('builder')!r}")
    args = entry.get("args")
    if not isinstance(args, list) or len(args) != n_args:
        raise _err(f"{builder} takes {n_args} argument(s), got {args!r}")
    for x in args:
        if isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(float(x)):
            raise _err(f"{builder} argument {x!r} is not a finite number")
        if float(x) <= 0.0:
            raise _err(f"{builder} argument {x!r} must be > 0 (power-family exponent n)")
    exch = entry.get("exchange")
    if exch not in EXCHANGE_BUILDERS:
        raise _err(f"exchange {exch!r} is not one of {sorted(EXCHANGE_BUILDERS)}")
    if entry.get("exchange_builder") != EXCHANGE_BUILDERS[exch]:
        raise _err(f"exchange {exch} uses {EXCHANGE_BUILDERS[exch]}, not {entry.get('exchange_builder')!r}")
    if entry.get("role") not in ROLES:
        raise _err(f"role {entry.get('role')!r} is not one of {list(ROLES)}")
    if check_id:
        want = model_id(fam, exch, [float(x) for x in args])
        if entry.get("model_id") != want:
            raise _err(f"model_id {entry.get('model_id')!r} does not match its family/args/exchange ({want})")


def document(models: Iterable[Mapping[str, Any]], queuetruth_version: str) -> dict[str, Any]:
    """The whole file; model ids must be unique."""
    ms = [dict(m) for m in models]
    seen: set[str] = set()
    for m in ms:
        validate_entry(m)
        if m["model_id"] in seen:
            raise _err(f"duplicate model_id {m['model_id']}")
        seen.add(m["model_id"])
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "queuetruth_version": queuetruth_version,
        "models": ms,
    }


def dumps(doc: Mapping[str, Any]) -> str:
    """Canonical JSON text (sorted keys, indent 2, trailing newline): byte-identical for identical input."""
    return json.dumps(doc, sort_keys=True, indent=2, allow_nan=False) + "\n"


def load(path: str | Path) -> dict[str, Any]:
    """Read and validate a config file."""
    try:
        doc = json.loads(Path(path).read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"QT-INPUT-001: config file not found: {path}") from e
    except json.JSONDecodeError as e:
        raise _err(f"{path} is not JSON: {e}") from e
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise _err(f"{path}: schema is not {SCHEMA}")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise _err(f"{path}: schema_version {doc.get('schema_version')!r} is not {SCHEMA_VERSION}")
    models = doc.get("models")
    if not isinstance(models, list) or not models:
        raise _err(f"{path}: models must be a non-empty list")
    return document(models, str(doc.get("queuetruth_version", "")))


def select(doc: Mapping[str, Any], model_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Entries by model id (all when `model_ids` is None); an unknown id is an error."""
    models = {m["model_id"]: m for m in doc["models"]}
    if model_ids is None:
        return list(models.values())
    out = []
    for mid in model_ids:
        if mid not in models:
            raise _err(f"model_id {mid!r} is not in the config (have {sorted(models)})")
        out.append(models[mid])
    return out


def apply(asset: Any, entry: Mapping[str, Any]) -> Any:
    """Apply the entry's queue model and exchange to an hftbacktest `BacktestAsset` and return it.

    Usage in the hftbacktest venv:

        asset = BacktestAsset().data(...).tick_size(...).lot_size(...)  # your settings
        asset = hbt_config.apply(asset, hbt_config.select(hbt_config.load(path), [model_id])[0])
    """
    validate_entry(entry)
    asset = getattr(asset, entry["builder"])(*[float(x) for x in entry["args"]])
    return getattr(asset, entry["exchange_builder"])()


def builder_chain(entry: Mapping[str, Any]) -> str:
    """The builder calls as text, e.g. `.power_prob_queue_model3(3.097579196829366).partial_fill_exchange()`."""
    validate_entry(entry)
    args = ", ".join(repr(float(x)) for x in entry["args"])
    return f".{entry['builder']}({args}).{entry['exchange_builder']}()"
