"""`replay`: the exact replay, and the `exclude_order_ids` resolution it shares with `zoo`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from queuetruth.errors import QtError
from queuetruth.ops._call import _call, _call_spec, _dump, _parse_doc, _validate
from queuetruth.ops.table import OperationDef
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as s

_REPLAY_OPTIONS = ("fill_rule", "hidden_print_policy", "exclude_order_ids")


def _instrumentless(item: Any) -> bool:
    """An `exclude_order_ids` item that names no instrument: a bare venue order id (integer or decimal string) or an
    object without `instrument_id`."""
    if isinstance(item, bool):
        return False
    if isinstance(item, int):
        return True
    if isinstance(item, str):
        return item.strip().isdecimal()
    return isinstance(item, dict) and item.get("instrument_id") is None and "order_id" in item


def _expand_order_ids(spec: Any) -> Any:
    """A spec given as a dict whose `exclude_order_ids` holds instrument-less items (bare venue order ids, accepted
    by the Python API): the list resolved with `resolve_order_ids` against the spec's dataset, each such item applying
    to every instrument of the dataset, exactly as the CLI's `--exclude-order-ids`. Anything else is returned as
    given (validation reports malformed items)."""
    if not isinstance(spec, dict):
        return spec
    items = spec.get("exclude_order_ids")
    if not isinstance(items, list | tuple) or not any(_instrumentless(x) for x in items):
        return spec
    ds = spec.get("dataset")
    path = ds.get("path") if isinstance(ds, dict) else getattr(ds, "path", None)
    if path is None:
        raise QtError(
            "QT-CONFIG-001",
            "exclude_order_ids: bare order ids apply to every instrument of the dataset, which needs a local "
            "dataset path",
            "give {instrument_id, order_id} items",
        )
    return {**spec, "exclude_order_ids": resolve_order_ids(list(items), str(path))}


def replay(
    spec: s.ReplaySpec | dict[str, Any],
    *,
    fill_rule: str | None = None,
    hidden_print_policy: str | None = None,
    exclude_order_ids: list[int | str | dict[str, int]] | None = None,
) -> m.ReplaySummary:
    """`replay`: exact replay writing the SPEC 5.1.6 outputs to `spec.out` (when set). The keyword options
    `fill_rule` (`exact` | `et`), `hidden_print_policy` (`ignore` | `behind` | `behind_and_through`; default per
    venue) and `exclude_order_ids` (audit A1: real orders removed from the tape before shadows are injected; items
    are `{instrument_id, order_id}` or a bare venue order id, which applies to every instrument of the dataset)
    replace the schema 1.1 `ReplaySpec` fields of those names before validation, so they enter `run_id` and the spec
    echo (E38 OC-34); a spec dict may also hold bare order ids in `exclude_order_ids` (expanded to every
    instrument with `resolve_order_ids`, as the CLI does)."""
    if fill_rule is not None or hidden_print_policy is not None or exclude_order_ids is not None:
        base = _dump(spec) if isinstance(spec, BaseModel) else dict(spec)
        if fill_rule is not None:
            base["fill_rule"] = fill_rule.replace("-", "_")
        if hidden_print_policy is not None:
            base["hidden_print_policy"] = hidden_print_policy.replace("-", "_")
        if exclude_order_ids is not None:
            base["exclude_order_ids"] = list(exclude_order_ids)
        spec = base
    sp = _validate(s.ReplaySpec, _expand_order_ids(spec))
    doc = _dump(sp)
    opts: dict[str, Any] = {k: doc[k] for k in _REPLAY_OPTIONS if doc.get(k) not in (None, [], "exact")}
    # Spec fields of these names (schema 1.1) also travel as options, so a core built before 1.1 still applies them.
    text = _call_spec("replay", sp, json.dumps(opts) if opts else None, movable=_REPLAY_OPTIONS)
    return _parse_doc(m.ReplaySummary, text, "queuetruth._core.replay")


def resolve_order_ids(value: str | Path | list[Any] | dict[str, Any], dataset: str | Path) -> list[dict[str, int]]:
    """`exclude_order_ids` items (`{instrument_id, order_id[, session_date]}`, sorted, deduplicated) of a file path
    (`.json` list, or one `id` / `instrument_id,order_id` per line, or CSV with an `order_id` header) or of a list
    of ids / objects; an item without an instrument applies to every instrument of `dataset` (as the replay
    option)."""
    if isinstance(value, str | Path):
        value = {"path": str(value)}
    items = json.loads(_call("resolve_order_ids", json.dumps(value), str(dataset)))
    return [{k: v for k, v in it.items() if v is not None} for it in items]


OPERATIONS = (
    OperationDef(
        "replay",
        s.ReplaySpec,
        replay,
        m.ReplaySummary,
        "Exact replay of a local dataset with optional shadows; returns qt.replay_summary.",
        cost="minutes",
        run_id=True,
        prepare=_expand_order_ids,
        tool="qt_local_run_replay",
        title="Run replay locally",
        service_kind="replay",
    ),
)
