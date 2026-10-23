"""High-level Python API (SPEC 4.3), used by the CLI, the service worker and MCP local mode.

Every function accepts a Spec model or a plain dict (validated; invalid specs raise `QtError("QT-CONFIG-001")`) and
returns the Pydantic summary model. Native work runs in `queuetruth._core` with the GIL released; its
`QtCoreError(code, message)` is re-raised as `QtError`. Hybrid operations:

- `run_zoo`: Rust run, then `queuetruth.eval.evaluate_zoo` on `zoo_predictions.parquet` with the planned-life
  columns of `zoo_shadows.parquet` (audit ruling A2); writes `zoo_errors.parquet` and rewrites `summary.json`
  with the leaderboard.
- `calibrate` (alloc, survival, nt_touch): Rust fit (`fit.json`), then the fitted and the default-parameter model
  on the test window through the zoo (`held_out/`), `evaluate_zoo` -> `held_out` / `baseline_held_out`, and the
  config exports -> `calibration.json`. `calibrate` (latency): `queuetruth.live.latency_stats` (restricted to the
  `LatencyFit` fields) + latency npz + exports.
- `why` with a live order: the order's first schedule leg from `queuetruth.live.to_schedule` (moved into its MBO
  twin's queue place as the scorer does), then `_core.why` with the order's own records removed (ruling A1);
  `why_explain` adds the zoo model's queue estimate per event, the first divergence and a conclusion.
"""

from __future__ import annotations

import datetime
import json
import re
import tempfile
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from queuetruth.errors import QtError
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as s
from queuetruth.schemas.arrow import ENGINE_VERSION, SCHEMAS
from queuetruth.schemas.io import write_table
from queuetruth.schemas.runid import run_id as _run_id
from queuetruth.schemas.runid import spec_echo

S = TypeVar("S", bound=BaseModel)
D = TypeVar("D", bound=BaseModel)

#: Fields of `qt.calibration_result.latency` (`queuetruth.live.latency_stats` returns more).
LATENCY_FIT_KEYS = ("entry_ns", "response_ns", "cancel_ns", "amend_ns", "clock_sync_assumed")


# ---- plumbing ---------------------------------------------------------------------------------------------------


def _core() -> Any:
    try:
        from queuetruth import _core as core
    except ImportError as e:  # pragma: no cover - only without a built extension
        raise QtError("QT-ENGINE-001", f"queuetruth._core is not available: {e}", "build it with `make develop`") from e
    return core


def _qt_error(e: BaseException) -> QtError:
    args = getattr(e, "args", ())
    code = getattr(e, "code", None) or (args[0] if len(args) >= 2 else "QT-ENGINE-001")
    message = getattr(e, "message", None) or (args[1] if len(args) >= 2 else str(e))
    return QtError(str(code), str(message))


def _call(name: str, *args: Any) -> Any:
    """`_core.<name>(*args)` with `QtCoreError` mapped to `QtError`."""
    core = _core()
    try:
        return getattr(core, name)(*args)
    except core.QtCoreError as e:
        raise _qt_error(e) from None


def _validate(model: type[S], spec: S | dict[str, Any]) -> S:
    if isinstance(spec, model):
        return spec
    try:
        if isinstance(spec, BaseModel):
            return model.model_validate(spec.model_dump(mode="json", by_alias=True))
        return model.model_validate(spec)
    except ValidationError as e:
        errors = json.loads(e.json(include_url=False))
        first = errors[0] if errors else {}
        loc = ".".join(str(x) for x in first.get("loc", ()))
        msg = f"{model.__name__}: {loc + ': ' if loc else ''}{first.get('msg', 'invalid spec')}"
        if len(errors) > 1:
            msg += f" (+{len(errors) - 1} more)"
        raise QtError("QT-CONFIG-001", msg, details={"errors": errors}) from None


def _dump(spec: BaseModel) -> dict[str, Any]:
    return spec.model_dump(mode="json", by_alias=True)


def _parse_doc(doc: type[D], text: str, origin: str) -> D:
    try:
        return doc.model_validate_json(text)
    except ValidationError as e:
        errors = json.loads(e.json(include_url=False))
        first = errors[0] if errors else {}
        loc = ".".join(str(x) for x in first.get("loc", ()))
        raise QtError(
            "QT-ENGINE-001",
            f"{origin} returned a document that does not match {doc.__name__}: {loc}: {first.get('msg', '')}",
            details={"errors": errors},
        ) from None


def _field_default(spec: BaseModel, key: str) -> Any:
    f = type(spec).model_fields.get(key)
    if f is None:
        return None
    v = f.get_default(call_default_factory=True)
    return v.model_dump(mode="json", by_alias=True) if isinstance(v, BaseModel) else v


def _call_spec(name: str, spec: BaseModel, *extra: Any, movable: tuple[str, ...] = ()) -> str:
    """`_core.<name>(spec JSON, *extra)`. A field the core's spec mirror does not know yet (Python schema ahead of
    the Rust build) is dropped when it holds its default, or when it is in `movable` (carried by `extra`); any other
    unknown field stays an error."""
    doc = _dump(spec)
    for _ in range(len(doc) + 1):
        try:
            return _call(name, json.dumps(doc), *extra)
        except QtError as e:
            hit = re.search(r"unknown field `([A-Za-z0-9_]+)`", e.message)
            key = hit.group(1) if hit else None
            if e.code != "QT-CONFIG-001" or key is None or key not in doc:
                raise
            if key not in movable and doc[key] != _field_default(spec, key):
                raise QtError(
                    "QT-CONFIG-001",
                    f"{key}: not supported by this queuetruth._core build (only its default is accepted)",
                    "rebuild the extension (make develop) or leave the field at its default",
                ) from None
            doc.pop(key)
    raise QtError("QT-ENGINE-001", f"queuetruth._core.{name}: spec negotiation did not converge")


def _native(name: str, spec: BaseModel, doc: type[D]) -> D:
    return _parse_doc(doc, _call_spec(name, spec), f"queuetruth._core.{name}")


def _path_of(ref: Any, what: str) -> str:
    p = getattr(ref, "path", None)
    if p is None and isinstance(ref, dict):
        p = ref.get("path")
    if not isinstance(p, str):
        raise QtError("QT-CONFIG-001", f"{what}: a local {{'path': ...}} reference is required here")
    return p


def _write_json(doc: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@contextmanager
def _out_dir(out: str | None, prefix: str) -> Iterator[Path]:
    """`out` as a directory, or a temporary directory removed afterwards."""
    if out:
        p = Path(out)
        p.mkdir(parents=True, exist_ok=True)
        yield p
        return
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        yield Path(tmp)


def _resolve_ref(ref: dict[str, Any]) -> str:
    from queuetruth.score._io import resolve_ref

    return resolve_ref(ref)


# ---- native operations ------------------------------------------------------------------------------------------


def ingest(spec: s.IngestSpec | dict[str, Any]) -> m.IngestSummary:
    """`ingest`: source files -> normalized dataset (every format of SPEC 4.2)."""
    return _native("ingest", _validate(s.IngestSpec, spec), m.IngestSummary)


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


def sample_shadows(spec: s.ShadowSampleSpec | dict[str, Any]) -> m.ShadowSampleSummary:
    """`shadows sample`."""
    return _native("sample_shadows", _validate(s.ShadowSampleSpec, spec), m.ShadowSampleSummary)


def mirror_real_orders(spec: s.MirrorSpec | dict[str, Any]) -> m.ShadowSampleSummary:
    """`shadows mirror-real`."""
    return _native("mirror_real_orders", _validate(s.MirrorSpec, spec), m.ShadowSampleSummary)


def extract_features(spec: s.FeatureSpec | dict[str, Any]) -> m.FeatureSummary:
    """`features`."""
    return _native("extract_features", _validate(s.FeatureSpec, spec), m.FeatureSummary)


def referee_fixtures(out: str) -> list[str]:
    """`referee fixtures`: writes the bug-class fixtures under `out`; returns their directories."""
    doc = _parse_doc(m.FixturesSummary, _call("referee_fixtures", str(out)), "queuetruth._core.referee_fixtures")
    return [f.path for f in doc.fixtures]


def fixtures_synth(out: str, venue: str = "cme", sessions: int = 2) -> m.FixturesSummary:
    """`fixtures synth`: the synthetic quickstart dataset (synth_small generator) with truth tables."""
    text = _call("fixtures_synth", str(out), venue, int(sessions))
    return _parse_doc(m.FixturesSummary, text, "queuetruth._core.fixtures_synth")


def run_id(spec: BaseModel | dict[str, Any]) -> str:
    """`run_id` of a spec (SPEC 7.3) computed by the Rust core; pass a Spec model (fully defaulted) or its dump."""
    doc = _dump(spec) if isinstance(spec, BaseModel) else spec
    return str(_call("run_id", json.dumps(doc)))


# ---- shadows from a live log ------------------------------------------------------------------------------------


def shadows_from_live(
    live: str, out: str, *, entry: str = "exchange_ack", latency_ns: int | None = None
) -> m.ShadowSampleSummary:
    """`shadows from-live`: the live log's schedule as `qt.shadow_spec` at `out`; priority-keeping amends (if any)
    go to `<out stem>_amends.parquet` next to it."""
    from queuetruth import live as qlive

    entry = entry.replace("-", "_")
    spec_t, amend_t = qlive.to_schedule(str(live), entry=entry, latency_ns=latency_ns)
    out_p = Path(out)
    files = [m.FileEntry.model_validate(write_table(spec_t, out_p, SCHEMAS["qt.shadow_spec"]))]
    if amend_t.num_rows:
        amend_p = out_p.with_name(out_p.stem + "_amends.parquet")
        files.append(m.FileEntry.model_validate(write_table(amend_t, amend_p, SCHEMAS["qt.shadow_amend"])))
    by_inst: dict[str, int] = {}
    for v in spec_t.column("instrument_id").to_pylist():
        by_inst[str(v)] = by_inst.get(str(v), 0) + 1
    by_place: dict[str, int] = {}
    for v in spec_t.column("offset_ticks").to_pylist():
        by_place[str(v)] = by_place.get(str(v), 0) + 1
    echo = {"live": {"path": str(live)}, "entry": entry, "latency_ns": latency_ns}
    return m.ShadowSampleSummary(
        spec=echo,
        n=spec_t.num_rows,
        by_instrument=dict(sorted(by_inst.items(), key=lambda kv: int(kv[0]))),
        by_placement=dict(sorted(by_place.items(), key=lambda kv: int(kv[0]))),
        files=files,
    )


# ---- zoo --------------------------------------------------------------------------------------------------------


def _planned_life(out: Path) -> tuple[pa.Table, list[str]]:
    """The `planned_life` input of `evaluate_zoo` from `zoo_shadows.parquet` (audit ruling A2). A planned end before
    the shadow's `ts_live` (e.g. an administrative end before activation) is clamped to `ts_live`: no horizon of
    that shadow has a defined label."""
    p = out / "zoo_shadows.parquet"
    if not p.exists():
        raise QtError("QT-INPUT-001", f"{p}: missing (the leaderboard needs the planned-life table, E38 OC-7)")
    t = pq.read_table(p)
    cols = ["shadow_id", "ts_cancel_planned", "ts_admin_end"]
    if not all(c in t.column_names for c in [*cols, "ts_live"]):
        raise QtError("QT-INPUT-002", f"{p}: lacks the planned-life columns {[*cols, 'ts_live']}")
    live = t.column("ts_live")
    notes = []
    out_cols = {"shadow_id": t.column("shadow_id")}
    for c in cols[1:]:
        col = t.column(c)
        early = pc.fill_null(pc.less(col, live), False)
        n = pc.sum(pc.cast(early, pa.int64())).as_py() or 0
        if n:
            notes.append(f"planned_life: {n} shadow(s) with {c} before ts_live clamped to ts_live")
        out_cols[c] = pc.if_else(early, live, col)
    return pa.table(out_cols), notes


def _evaluate(
    preds: pa.Table, out: Path, *, h_max_ns: int, bootstrap_b: int, seed: int, trajectory: pa.Table | None
) -> tuple[pa.Table, list[dict[str, Any]], list[str], dict[str, Any]]:
    from queuetruth.eval import aborted_shadows, evaluate_zoo, leaderboard_population

    planned, notes = _planned_life(out)
    errors, board = evaluate_zoo(
        preds,
        h_max_ns=h_max_ns,
        trajectory=trajectory,
        bootstrap_b=bootstrap_b,
        seed=seed,
        planned_life=planned,
    )
    population = {
        "n_excluded_hbt_312": len(aborted_shadows(preds)),
        "n_leaderboard": leaderboard_population(preds, planned, h_max_ns),
    }
    return errors, board, notes, population


def resolve_order_ids(value: str | Path | list[Any] | dict[str, Any], dataset: str | Path) -> list[dict[str, int]]:
    """`exclude_order_ids` items (`{instrument_id, order_id[, session_date]}`, sorted, deduplicated) of a file path
    (`.json` list, or one `id` / `instrument_id,order_id` per line, or CSV with an `order_id` header) or of a list
    of ids / objects; an item without an instrument applies to every instrument of `dataset` (as the replay
    option)."""
    if isinstance(value, str | Path):
        value = {"path": str(value)}
    items = json.loads(_call("resolve_order_ids", json.dumps(value), str(dataset)))
    return [{k: v for k, v in it.items() if v is not None} for it in items]


def run_zoo(spec: s.ZooSpec | dict[str, Any]) -> m.ZooSummary:
    """`zoo`: Rust run (`zoo_predictions`, `zoo_fills`, `zoo_trajectory`, `zoo_shadows`) + `queuetruth.eval`
    metrics (`zoo_errors.parquet`, leaderboard in `summary.json`). Bare order ids in a spec dict's
    `exclude_order_ids` apply to every instrument of the dataset (`resolve_order_ids`, as the CLI)."""
    sp = _validate(s.ZooSpec, _expand_order_ids(spec))
    with _out_dir(sp.out, "qt-zoo-") as out:
        run = sp.model_copy(update={"out": str(out)})
        summ = _native("run_zoo", run, m.ZooSummary)
        preds = pq.read_table(out / "zoo_predictions.parquet")
        traj_p = out / "zoo_trajectory.parquet"
        traj = pq.read_table(traj_p) if sp.write_trajectory and traj_p.exists() else None
        errors, board, notes, population = _evaluate(
            preds, out, h_max_ns=sp.h_max_ns, bootstrap_b=sp.bootstrap_b, seed=sp.seed, trajectory=traj
        )
        entry = m.FileEntry.model_validate(write_table(errors, out / "zoo_errors.parquet"))
        files = [f for f in summ.files if f.name != entry.name] + [entry]
        summary = summ.model_copy(
            update={
                "leaderboard": [m.LeaderboardEntry.model_validate(x) for x in board],
                "files": files,
                "warnings": [*summ.warnings, *notes],
                **population,
            }
        )
        summary = m.ZooSummary.model_validate(summary.model_dump(mode="json"))
        if sp.out:
            _write_json(summary.model_dump(mode="json"), out / "summary.json")
    return summary


# ---- calibration ------------------------------------------------------------------------------------------------


def _model_ids(family: str, estimate: float) -> tuple[str, str]:
    from queuetruth.score.calibrate_live import DEFAULTS, model_id

    return model_id(family, estimate), model_id(family, DEFAULTS[family])


def _instrument_index(dataset: str) -> dict[tuple[int, int], tuple[int, int, int]]:
    """(instrument_id, session_date days) -> (midnight_utc_ns, open_s, close_s)."""
    t = pq.read_table(Path(dataset) / "instruments.parquet")
    days = pc.cast(t.column("session_date"), pa.int32()).to_pylist()
    out = {}
    for i, iid in enumerate(t.column("instrument_id").to_pylist()):
        out[(int(iid), int(days[i]))] = (
            int(t.column("midnight_utc_ns")[i].as_py()),
            int(t.column("open_s")[i].as_py()),
            int(t.column("close_s")[i].as_py()),
        )
    return out


def _window_mask(preds: pa.Table, window: s.WindowSpec, dataset: str) -> list[bool]:
    """Per prediction row: the shadow belongs to the window (session date; block of `ts_live` among `n_blocks`
    equal-duration blocks of the continuous session, as `qt_calib`)."""
    n = preds.num_rows
    if window.dates is None and window.blocks is None:
        return [True] * n
    days = pc.cast(preds.column("session_date"), pa.int32()).to_pylist()
    iids = preds.column("instrument_id").to_pylist()
    live = pc.cast(preds.column("ts_live"), pa.int64()).to_pylist()
    want_days = None
    if window.dates is not None:
        epoch = datetime.date(1970, 1, 1)
        want_days = {(datetime.date.fromisoformat(d) - epoch).days for d in window.dates}
    index = _instrument_index(dataset) if window.blocks is not None else {}
    mask = []
    for i in range(n):
        ok = want_days is None or int(days[i]) in want_days
        if ok and window.blocks is not None:
            nb = window.blocks.n_blocks
            inst = index.get((int(iids[i]), int(days[i])))
            if inst is None:
                blk = 0
            else:
                midnight, open_s, close_s = inst
                open_ns = midnight + open_s * 1_000_000_000
                length = max(close_s - open_s, 1) * 1_000_000_000
                pos = float(live[i] - open_ns) / float(length)
                blk = min(max(int(pos * nb // 1), 0), nb - 1)
            ok = blk in set(window.blocks.index)
        mask.append(ok)
    return mask


def _held(per_h: dict[str, dict[str, Any]], keys: list[str]) -> m.HeldOut:
    """`HeldOut` from per-horizon leaderboard entries (`{h: entry}`); null where a horizon has no defined label."""

    def pick(metric: str, h: str) -> float | None:
        v = (per_h.get(h) or {}).get(metric, {}).get(h)
        return v.get("value") if isinstance(v, dict) else v

    return m.HeldOut(
        brier={h: pick("brier", h) for h in keys},
        logloss={h: pick("logloss", h) for h in keys},
        fill_rate_bias={h: pick("fill_rate_bias", h) for h in keys},
    )


def _held_out(
    sp: s.CalibrationSpec, fit: m.CalibrationResult, out: Path
) -> tuple[dict[str, m.HeldOut], dict[str, m.HeldOut], list[str]]:
    if fit.estimate is None:
        return {}, {}, ["held_out: no estimate; held-out evaluation skipped"]
    fitted, baseline = _model_ids(sp.family, fit.estimate)
    dataset = _path_of(sp.dataset, "dataset")
    zoo = s.ZooSpec(
        dataset=sp.dataset,
        shadows=sp.shadows,
        out=str(out / "held_out"),
        models=[fitted, baseline],
        mode=sp.mode,
        exchange=sp.exchange,
        horizons_ns=sp.horizons_ns,
        h_max_ns=sp.h_max_ns,
        l2=sp.l2,
        particles=sp.particles,
        seed=sp.seed,
        write_fills=False,
        threads=sp.threads,
        bootstrap_b=sp.bootstrap.b,
    )
    summ = _native("run_zoo", zoo, m.ZooSummary)
    ids = list(summ.models)
    fitted_id = ids[0] if ids else fitted
    base_id = ids[1] if len(ids) > 1 else fitted_id
    preds = pq.read_table(out / "held_out" / "zoo_predictions.parquet")
    preds = preds.filter(pa.array(_window_mask(preds, sp.test, dataset), pa.bool_()))
    warn = [f"held_out: fitted {fitted_id} vs default {base_id} on {preds.num_rows} test prediction rows"]
    # faithful hbt #312: a shadow either model aborted on is left out of both models' held-out metrics (one common
    # population, `queuetruth.eval.common_population`)
    warn += [f"held_out: {w}" for w in summ.warnings if w.startswith("hbt_312_abort: ")]
    if preds.num_rows == 0:
        return {}, {}, [*warn, "held_out: the test window holds no shadows"]
    # Per horizon h, the leaderboard population is the shadows whose planned life covers h (A2 labels exist iff the
    # planned end is at or after entry + h); evaluating each horizon on its own keeps short-lived schedules usable.
    hcol = pc.cast(preds.column("horizon_ns"), pa.int64())
    keys = [str(h) for h in sorted({int(x) for x in hcol.to_pylist()})]
    per_model: dict[str, dict[str, dict[str, Any]]] = {}
    undefined = []
    for h in keys:
        preds_h = preds.filter(pc.equal(hcol, int(h)))
        _, board, notes, _ = _evaluate(
            preds_h, out / "held_out", h_max_ns=int(h), bootstrap_b=sp.bootstrap.b, seed=sp.seed, trajectory=None
        )
        warn += [n for n in notes if n not in warn]
        for e in board:
            per_model.setdefault(e["model_id"], {})[h] = e
        if not any((e.get("brier") or {}).get(h) is not None for e in board):
            undefined.append(h)
    if undefined:
        warn.append(f"held_out: no shadow's planned life covers horizon(s) {', '.join(undefined)} ns (metrics null)")
    held = {fitted_id: _held(per_model[fitted_id], keys)} if fitted_id in per_model else {}
    base = {base_id: _held(per_model[base_id], keys)} if base_id in per_model else {}
    if sp.test.dates is None and sp.test.blocks is None:
        warn.append("held_out: no test window given; evaluated on all data (includes the train window)")
    return held, base, warn


def _calibrate_latency(sp: s.CalibrationSpec) -> m.CalibrationResult:
    from queuetruth import live as qlive
    from queuetruth.export import export_snippets, write_latency_npz

    live_dir = _path_of(sp.live, "live")
    stats = qlive.latency_stats(live_dir)
    lat = m.LatencyFit.model_validate({k: stats[k] for k in LATENCY_FIT_KEYS})
    warns = ["clock_sync_assumed: one-way latencies need synchronized clocks"]
    for k, v in sorted((stats.get("negative_dropped") or {}).items()):
        if v:
            warns.append(f"{k}: {v} negative value(s) dropped")
    doc = _dump(sp)
    rid = _run_id(doc, ENGINE_VERSION, _resolve_ref)
    npz: str | None = None
    with _out_dir(sp.out, "qt-latency-") as out:
        if sp.out:
            try:
                write_latency_npz(live_dir, str(out / "latency.npz"))
                npz = "latency.npz"
            except QtError as e:
                warns.append(f"latency_npz: not written ({e})")
        cal = m.CalibrationResult(
            run_id=rid,
            spec=spec_echo(doc),
            kind="latency",
            family="latency",
            mode=sp.mode,
            n_events=int(lat.entry_ns.n),
            n_shadows=int(stats.get("n_orders", 0) or 0),
            train=m.WindowEcho.model_validate(sp.train.model_dump(mode="json")),
            test=m.WindowEcho.model_validate(sp.test.model_dump(mode="json")),
            held_out={},
            baseline_held_out={},
            stratified=[],
            latency=lat,
            exports=m.Exports(latency_npz=npz),
            data_sources=[],
            warnings=warns,
        )
        cal = _annotate_trust(cal)
        cal = cal.model_copy(update={"exports": export_snippets(cal, npz)})
        if sp.out:
            _write_json(cal.model_dump(mode="json"), out / "calibration.json")
    return cal


def _annotate_trust(cal: m.CalibrationResult) -> m.CalibrationResult:
    """The calibration with the export's fail-safe verdict appended to its warnings (`trust: beats_default: ...`,
    `trust: verdict: exportable|refused (...)`; `queuetruth.export.trust.annotate`), before the snippets are made."""
    try:
        from queuetruth.export.trust import annotate
    except ImportError:  # a build without the fail-safe assessment
        return cal
    return annotate(cal)


def calibrate(spec: s.CalibrationSpec | dict[str, Any]) -> m.CalibrationResult:
    """`calibrate`: alloc / survival / nt_touch (Rust fit + held-out zoo evaluation + `trust:` verdict + exports,
    `calibration.json`) or latency (Python)."""
    from queuetruth.export import export_snippets

    sp = _validate(s.CalibrationSpec, spec)
    if sp.kind == "latency":
        return _calibrate_latency(sp)
    with _out_dir(sp.out, "qt-calibrate-") as out:
        run = sp.model_copy(update={"out": str(out)})
        fit = _native("calibrate", run, m.CalibrationResult)
        held, base, warns = _held_out(sp, fit, out)
        kept = [w for w in fit.warnings if not w.startswith("rust part only")]
        cal = fit.model_copy(update={"held_out": held, "baseline_held_out": base, "warnings": kept + warns})
        cal = _annotate_trust(cal)
        cal = cal.model_copy(update={"exports": export_snippets(cal)})
        cal = m.CalibrationResult.model_validate(cal.model_dump(mode="json"))
        if sp.out:
            _write_json(cal.model_dump(mode="json"), out / "calibration.json")
    return cal


def calibrate_live(spec: Any, **kwargs: Any) -> m.CalibrationResult:
    """`calibrate-live`: fit a zoo parameter to the user's own live fills (`queuetruth.score.calibrate_live`)."""
    from queuetruth.score import calibrate_live as _calibrate_live

    return _calibrate_live(spec, **kwargs)


# ---- Python-side operations -------------------------------------------------------------------------------------


def fit_hazard(spec: s.HazardFitSpec | dict[str, Any]) -> m.HazardModelCard:
    """`fit-hazard` (`queuetruth.model.fit_hazard`)."""
    from queuetruth.model import fit_hazard as _fit

    sp = _validate(s.HazardFitSpec, spec)
    # SPEC 4.4 `--features DIR`: a features output directory resolves to its `features.parquet`
    path = getattr(sp.features, "path", None)
    if path is not None and Path(path).is_dir() and (Path(path) / "features.parquet").is_file():
        sp = sp.model_copy(update={"features": s.PathRef(path=str(Path(path) / "features.parquet"))})
    return _fit(sp)


def import_live(
    spec: s.LiveImportSpec | dict[str, Any], *, tz: str | None = None, **options: Any
) -> m.LiveImportSummary:
    """`live import`: venue exports (`queuetruth.live.import_venue`) or simulator reports (venues `sim_hftbacktest`
    / `sim_nautilus`: `queuetruth.live.import_sim`, log venue `sim:<name>`, for `score --sim-log`). `tz` (default:
    the spec's `tz`) is the account time zone of venue-local times (IBKR Flex). `options` go to the simulator
    importer (`venue` = live venue of the instrument-map rows, `size_exp`, `key_from_client_id`,
    `order_key_map`)."""
    from queuetruth import live as qlive

    sp = _validate(s.LiveImportSpec, spec)
    tz = tz if tz is not None else sp.tz
    if sp.venue.startswith("sim_") and sp.venue not in qlive.IMPORTERS:
        if not sp.out:
            raise QtError("QT-CONFIG-001", "out: an output directory is required")
        if tz is not None:
            raise QtError("QT-CONFIG-001", "tz: simulator reports carry UTC epoch timestamps; tz does not apply")
        inputs = [_path_of(r, "inputs") for r in sp.inputs]
        imap = _path_of(sp.instrument_map, "instrument_map") if sp.instrument_map is not None else None
        return qlive.import_sim(
            sp.venue.removeprefix("sim_"),
            inputs,
            sp.out,
            account=sp.account,
            instrument_map=imap,
            append=sp.append,
            **options,
        )
    if options:
        raise QtError(
            "QT-CONFIG-001", f"options {sorted(options)}: apply to simulator imports (sim_hftbacktest, sim_nautilus)"
        )
    return qlive.import_venue(sp, tz=tz)


def validate_live(path: str) -> list[str]:
    """`live validate`: warnings; raises `QtError` on errors."""
    from queuetruth import live as qlive

    return list(qlive.validate(path))


def score(spec: s.ScorecardSpec | dict[str, Any], **kwargs: Any) -> m.ScorecardSummary:
    """`score` (`queuetruth.score.build_scorecard`; keyword extras `window_ns`, `match_window_ns`, `self_exclude`).
    Failed gates are reported in `verdict.gates_passed` (the CLI exits 1)."""
    from queuetruth.score import build_scorecard

    return build_scorecard(_validate(s.ScorecardSpec, spec), **kwargs)


def report(spec: s.ReportSpec | dict[str, Any]) -> m.ReportSummary:
    """`report` (`queuetruth.report.render`)."""
    from queuetruth.report import render

    return render(_validate(s.ReportSpec, spec))


def dq(spec: Any) -> Any:
    """`dq`: the data-quality battery of `queuetruth.dq` over one dataset (and, with `compare`, a second dataset of
    the same sessions). `spec` is a `queuetruth.dq.DqSpec` or a dict of its fields; returns the `qt.dq_report`
    (`queuetruth.dq.DqReport`) and, with `spec.out`, writes `dq_report.json` and `dq_report.html` there."""
    from queuetruth.dq import run

    return run(spec)


def referee(spec: s.RefereeSpec | dict[str, Any]) -> m.RefereeReport:
    """`referee run` (`queuetruth.score.run_referee`)."""
    from queuetruth.score import run_referee

    return run_referee(_validate(s.RefereeSpec, spec))


def _live_leg(live_dir: str, order_key: str) -> pa.Table:
    from queuetruth import live as qlive

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sched, _ = qlive.to_schedule(live_dir, entry="exchange_ack", latency_ns=None)
    keys = sched.column("order_key").to_pylist() if "order_key" in sched.column_names else []
    idx = [i for i, k in enumerate(keys) if k == order_key]
    if not idx:
        raise QtError(
            "QT-CONFIG-001",
            f"order_key: {order_key!r} has no schedulable leg in {live_dir}",
            "market/IOC orders and orders without an entry time are not scheduled",
        )
    ts = pc.cast(sched.column("ts_submit"), pa.int64()).to_pylist()
    first = min(idx, key=lambda i: (ts[i], i))
    return sched.slice(first, 1)


# ---- why: the exact trace, the model next to it, and a conclusion -----------------------------------------------

_NS = 1_000_000_000


def dataset_kind(dataset: str | Path) -> str | None:
    """`kind` of a dataset directory's manifest (`mbo` or `l2`); None when it cannot be read."""
    try:
        doc = json.loads((Path(dataset) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    k = doc.get("kind") if isinstance(doc, dict) else None
    return str(k) if k is not None else None


def why_suggestion(
    dataset: str,
    *,
    shadows: str | None = None,
    shadow_id: str | None = None,
    live: str | None = None,
    order_key: str | None = None,
    model: str | None = None,
) -> str | None:
    """A `queuetruth` command line that explains one order on this dataset, or None when the dataset cannot be read.

    On MBO data it is `queuetruth why` (with `--model` when given). An exact queue trace needs MBO, so on an L2-only
    dataset `why` cannot run; the suggestion is then the model's queue trajectory for that order
    (`zoo --write-trajectory`; a live order is turned into a shadow first with `shadows from-live`)."""
    import shlex

    kind = dataset_kind(dataset)
    if kind is None:
        return None
    q = shlex.quote
    if kind != "l2":
        if live is not None and order_key is not None:
            cmd = f"queuetruth why --data {q(dataset)} --live {q(live)} --order-key {q(order_key)}"
        elif shadows is not None and shadow_id is not None:
            cmd = f"queuetruth why --data {q(dataset)} --shadows {q(shadows)} --shadow-id {q(shadow_id)}"
        else:
            return None
        return cmd + (f" --model {q(model)}" if model else "")
    m_id = model or "hbt_power3[n=2]"
    if live is not None and order_key is not None:
        return (
            f"queuetruth shadows from-live --live {q(live)} --out own_orders.parquet && queuetruth zoo --data "
            f"{q(dataset)} --shadows own_orders.parquet --models {q(m_id)} --write-trajectory --out why_zoo "
            f"(L2-only data: no exact queue; read rows with shadow_id {q(order_key)} in why_zoo/zoo_trajectory.parquet)"
        )
    if shadows is not None:
        sid = f" (shadow_id {q(shadow_id)})" if shadow_id else ""
        return (
            f"queuetruth zoo --data {q(dataset)} --shadows {q(shadows)} --models {q(m_id)} --write-trajectory "
            f"--out why_zoo (L2-only data: no exact queue; read why_zoo/zoo_trajectory.parquet{sid})"
        )
    return None


class WhyExplanation(BaseModel):
    """Result of `why_explain` (a Python object, not a `qt.*` document).

    - `trace`: the exact engine's `qt.why_trace`; with a model, each event's `note` also carries `model_ahead=<x>`
      and the first divergent event `first_divergence: ...`.
    - `symbol`, `price_exp`, `size_exp`, `tick_size`: the instrument's units, for display.
    - `model_id`, `model_queue_ahead`: the zoo model's queue-ahead estimate as of each trace event (aligned with
      `trace.events`; None before the model's first estimate).
    - `first_divergence`: index of the first event after which model and exact queue ahead differ by at least
      `divergence_tol` (compared at the end of each event batch); None when they never do (or no model).
    - `excluded_order_ids`: the MBO order ids of the user's own order removed from the tape (live orders, A1).
    - `conclusion`: one plain sentence; `notes`: caveats (also emitted as warnings by `why`).
    """

    model_config = ConfigDict(extra="forbid")

    trace: m.WhyTrace
    symbol: str | None = None
    price_exp: int | None = None
    size_exp: int | None = None
    tick_size: int | None = None
    model_id: str | None = None
    model_queue_ahead: list[float | None] = Field(default_factory=list)
    first_divergence: int | None = None
    divergence_tol: float | None = None
    excluded_order_ids: list[int] = Field(default_factory=list)
    conclusion: str = ""
    notes: list[str] = Field(default_factory=list)


def _instrument_units(root: Path, iid: int, ts: int | None) -> dict[str, Any]:
    """symbol / price_exp / size_exp / tick_size of an instrument on the session of `ts` (any session otherwise)."""
    try:
        t = pq.read_table(root / "instruments.parquet")
    except (OSError, pa.ArrowInvalid):
        return {}
    rows = [r for r in t.to_pylist() if int(r.get("instrument_id", -1)) == int(iid)]
    if not rows:
        return {}
    pick = rows[0]
    if ts is not None:
        for r in rows:
            mid = r.get("midnight_utc_ns")
            if mid is not None and int(mid) <= ts < int(mid) + 86_400 * _NS:
                pick = r
                break
    return {
        "symbol": pick.get("symbol"),
        "price_exp": pick.get("price_exp"),
        "size_exp": pick.get("size_exp"),
        "tick_size": pick.get("tick_size"),
    }


def fmt_utc(ns: int | None, digits: int = 6, date: bool = False) -> str:
    """`HH:MM:SS.ffffff` (UTC) of an epoch-ns timestamp; with `date`, `YYYY-MM-DD HH:MM:SS.ffffff`."""
    if ns is None:
        return "-"
    sec, frac = divmod(int(ns), _NS)
    t = datetime.datetime.fromtimestamp(sec, tz=datetime.UTC)
    base = t.strftime("%Y-%m-%d %H:%M:%S" if date else "%H:%M:%S")
    return base + (f".{frac:09d}"[: digits + 1] if digits > 0 else "")


def fmt_scaled(v: float | int | None, exp: int | None) -> str:
    """A fixed-point integer `v * 10**exp` as a decimal string (no exponent; trailing zeros trimmed)."""
    if v is None:
        return "-"
    if exp is None or exp == 0:
        return f"{v:g}" if isinstance(v, float) else str(v)
    from decimal import Decimal

    d = Decimal(str(v)).scaleb(int(exp))
    text = format(d.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".")


def fmt_dur(ns: int | None) -> str:
    """Duration in readable units (`250 ms`, `52.2 s`, `4 min 51 s`)."""
    if ns is None:
        return "-"
    a = abs(int(ns))
    if a < 1_000:
        return f"{ns} ns"
    if a < 1_000_000:
        return f"{ns / 1e3:.3g} us"
    if a < _NS:
        return f"{ns / 1e6:.3g} ms"
    if a < 60 * _NS:
        return f"{ns / _NS:.3g} s"
    mins, rest = divmod(a, 60 * _NS)
    return f"{'-' if ns < 0 else ''}{mins} min {rest // _NS} s"


_FILL_REASONS = {
    "queue_depleted": "nothing was left ahead of you when a trade reached your price",
    "trade_through": "a trade printed through your price",
    "crossed": "the opposite side crossed your price (a taker fill)",
    "crossed_at_live": "your order was marketable on arrival (a taker fill)",
    "hidden_print": "a hidden execution printed at your price with nothing displayed ahead",
}


def _batches(events: list[m.WhyEvent]) -> list[list[int]]:
    """Indices of the events grouped by record batch (same `ts_event` and `sequence`), in order."""
    out: list[list[int]] = []
    key = None
    for i, e in enumerate(events):
        k = (e.ts_event, e.sequence)
        if k != key:
            out.append([])
            key = k
        out[-1].append(i)
    return out


def _queue_story(trace: m.WhyTrace, upto: int | None, size: Callable[[int], str]) -> tuple[int, int, str]:
    """(queue ahead when the order went live, after event `upto` (default: the last), and "x cancelled, y traded"
    for what left the queue ahead of it). Rows of one record batch all carry the queue after the batch; the lots
    that left are read from the batch's records ahead: a `C` of an order executed (`F`) in the same batch is
    traded, any other `C` cancelled, the rest of the drop modified away (priority-losing `M`) or cleared."""
    ev = trace.events
    if not ev:
        return 0, 0, ""
    last = len(ev) - 1 if upto is None else upto
    left = {"cancelled": 0, "traded": 0, "modified away": 0, "cleared": 0}
    q0: int | None = None
    prev = 0
    qa = 0
    for b in _batches(ev):
        if b[0] > last:
            break
        rows = [ev[i] for i in b]
        qa = rows[-1].queue_ahead_after
        executed = {e.order_id for e in rows if e.action == "F"}
        reduced = {e.order_id for e in rows if e.action == "C"}
        traded = cancelled = 0
        for e in rows:
            if not e.is_ahead:
                continue
            if e.action == "C" and e.effect in ("remove", "reduce"):
                if e.order_id in executed:
                    traded += e.size
                else:
                    cancelled += e.size
            elif e.action == "F" and e.order_id not in reduced:
                traded += e.size
        if q0 is None:
            q0 = prev = qa + traded + cancelled
        drop = max(0, prev - qa)
        prev = qa
        t = min(traded, drop)
        c = min(cancelled, drop - t)
        rest = drop - t - c
        left["traded"] += t
        left["cancelled"] += c
        if rest:
            left["cleared" if any(e.action == "R" for e in rows) else "modified away"] += rest
    parts = [f"{size(v)} {k}" for k, v in left.items() if v]
    return q0 or 0, qa, ", ".join(parts)


def _conclusion(x: WhyExplanation) -> str:
    """One plain sentence: why the order filled (or not), then where the model first went wrong."""
    t = x.trace
    ev = t.events

    def size(v: float | int) -> str:
        return fmt_scaled(v, x.size_exp)

    start = t.ts_live
    fill_i = next((i for i, e in enumerate(ev) if e.fill_qty > 0), None)
    if t.outcome.filled_qty > 0 and fill_i is not None:
        fe = ev[fill_i]
        reasons = list(dict.fromkeys(re.findall(r"fill \S+ ([a-z_]+)", fe.note or ""))) or ["fill"]
        q0, q1, story = _queue_story(t, fill_i, size)
        batch = [e for e in ev if (e.ts_event, e.sequence) == (fe.ts_event, fe.sequence)]
        prints = [e for e in batch if e.action == "T"]
        execs = [e for e in batch if e.action == "F"]
        what = []
        if prints:
            sizes = ", ".join(size(e.size) for e in prints[:4])
            what.append(f"{'a trade' if len(prints) == 1 else 'trades'} of {sizes} printed")
        if execs:
            who = ", ".join(
                f"order {e.order_id} ({size(e.size)}, {'ahead of' if e.is_ahead else 'behind'} you)" for e in execs[:3]
            )
            what.append(f"{'executing ' if prints else ''}{who}{'' if prints else ' traded'}")
        trig = f" at {fmt_utc(fe.ts_event, 3, date=True)} UTC" + (f" when {', '.join(what)}" if what else "")
        why_txt = "; ".join(_FILL_REASONS.get(r, r.replace("_", " ")) for r in reasons)
        span = fmt_dur(fe.ts_event - start) if start is not None else "-"
        text = (
            f"Filled {size(t.outcome.filled_qty)} of {size(t.qty)}{trig}: {why_txt}. Queue ahead went "
            f"{size(q0)} -> {size(q1)} over {span}" + (f" ({story})." if story else ".")
        )
    elif not ev:
        text = f"Not filled (end state {t.outcome.end_state}); nothing happened at the order's price while it was live."
    else:
        q0, q1, story = _queue_story(t, None, size)
        span = fmt_dur(ev[-1].ts_event - start) if start is not None else "-"
        text = (
            f"Not filled (end state {t.outcome.end_state}): queue ahead went {size(q0)} -> {size(q1)} over {span}"
            + (f" ({story})" if story else "")
            + (f"; {size(q1)} still ahead at the end." if q1 > 0 else "; no trade reached the price after that.")
        )
    if x.model_id is not None:
        first_batch = _batches(ev)[0] if ev else []
        q_start = _queue_story(t, first_batch[-1], size)[0] if ev else 0
        est0 = x.model_queue_ahead[x.first_divergence] if x.first_divergence is not None else None
        if x.first_divergence in first_batch and est0 is not None and abs(est0 - q_start) >= (x.divergence_tol or 1.0):
            text += (
                f" Model {x.model_id} was off from the start: when the order went live it assumed "
                f"{size(round(est0, 6))} ahead where the exact queue had {size(q_start)}."
            )
        elif x.first_divergence is not None and x.first_divergence < len(ev):
            d = ev[x.first_divergence]
            est = x.model_queue_ahead[x.first_divergence]
            text += (
                f" Model {x.model_id} first diverged at {fmt_utc(d.ts_event, 3)}: it had "
                f"{size(round(est, 6)) if est is not None else '-'} ahead where the exact queue had "
                f"{size(d.queue_ahead_after)} (after {d.action} of order {d.order_id}, "
                f"{'ahead of' if d.is_ahead else 'behind'} you)."
            )
        elif any(v is not None for v in x.model_queue_ahead):
            text += f" Model {x.model_id} stayed within {size(x.divergence_tol or 0)} of the exact queue."
        else:
            text += f" Model {x.model_id}: no queue estimate for this order."
    return text


def _model_matches(want: str, got: str) -> bool:
    """`hbt_power3[n=2]` matches `hbt_power3[exch=partial;mode=faithful;n=2]` (name equal, given params present)."""
    if want == got:
        return True

    def split(mid: str) -> tuple[str, set[str]]:
        name, _, rest = mid.partition("[")
        params = {p.strip() for p in rest.rstrip("]").replace(",", ";").split(";") if p.strip()}
        return name.strip(), params

    wn, wp = split(want)
    gn, gp = split(got)
    if wn != gn:
        return False
    canon = {p.replace(" ", "") for p in gp}
    for p in wp:
        k, _, v = p.partition("=")
        if p.replace(" ", "") in canon:
            continue
        try:
            fv = float(v)
        except ValueError:
            return False
        if not any(q.partition("=")[0] == k.strip() and _float_or_none(q.partition("=")[2]) == fv for q in canon):
            return False
    return True


def _float_or_none(v: str) -> float | None:
    try:
        return float(v)
    except ValueError:
        return None


def _traj_rows(t: pa.Table, sid: str) -> pa.Table:
    t = t.filter(pc.equal(t.column("shadow_id"), sid))
    ts = pc.cast(t.column("ts_event"), pa.int64())
    return t.set_column(t.schema.get_field_index("ts_event"), "ts_event", ts)


def _pick_model(rows: pa.Table, model: str | None) -> tuple[str | None, pa.Table]:
    """The trajectory rows of one model: `model` when given, else the model that strays furthest from the exact
    queue on this shadow (the one worth explaining)."""
    ids = sorted({str(v) for v in rows.column("model_id").to_pylist()})
    if model is not None:
        ids = [i for i in ids if _model_matches(model, i)]
        if not ids:
            return None, rows.slice(0, 0)
        mid = ids[0]
    elif not ids:
        return None, rows
    else:

        def err(i: str) -> float:
            r = rows.filter(pc.equal(rows.column("model_id"), i))
            est = r.column("queue_ahead_est").to_pylist()
            ex = r.column("queue_ahead_exact").to_pylist()
            return max((abs(float(a) - float(b)) for a, b in zip(est, ex, strict=True) if a is not None), default=0.0)

        mid = max(ids, key=lambda i: (err(i), i))
    return mid, rows.filter(pc.equal(rows.column("model_id"), mid))


def _exact_mismatches(trace: m.WhyTrace, rows: pa.Table) -> int:
    """Trajectory rows whose `queue_ahead_exact` differs from this trace's exact queue at that time (rows of another
    tape or schedule must not be shown next to it)."""
    import bisect

    ev = trace.events
    if not ev or "queue_ahead_exact" not in rows.column_names:
        return 0
    q0 = _queue_story(trace, _batches(ev)[0][-1], str)[0]
    ts = [e.ts_event for e in ev]
    bad = 0
    for t_, ex in zip(rows.column("ts_event").to_pylist(), rows.column("queue_ahead_exact").to_pylist(), strict=True):
        if ex is None or (trace.ts_live is not None and int(t_) < trace.ts_live):
            continue
        j = bisect.bisect_right(ts, int(t_)) - 1
        want = ev[j].queue_ahead_after if j >= 0 else q0
        bad += int(int(ex) != int(want))
    return bad


def _zoo_ts_live(zoo: Path, sid: str) -> int | None:
    """`ts_live` of a shadow in a zoo output's `zoo_shadows.parquet` (None when unknown)."""
    try:
        t = pq.read_table(zoo / "zoo_shadows.parquet", columns=["shadow_id", "ts_live"])
    except (OSError, pa.ArrowInvalid, KeyError):
        return None
    t = t.filter(pc.equal(t.column("shadow_id"), sid))
    return int(pc.cast(t.column("ts_live"), pa.int64())[0].as_py()) if t.num_rows else None


def _model_run(
    root: Path, leg: pa.Table, model: str, excluded: list[tuple[int, int]], h_max_ns: int, notes: list[str]
) -> pa.Table | None:
    """The model's trajectory for one schedule leg from a one-shadow zoo run (own order removed from the L2 view
    when `excluded` is given and the core supports it)."""
    with tempfile.TemporaryDirectory(prefix="qt-why-zoo-") as tmp:
        sched = Path(tmp) / "leg.parquet"
        write_table(leg, sched, SCHEMAS["qt.shadow_spec"])
        base: dict[str, Any] = {
            "dataset": {"path": str(root)},
            "shadows": {"path": str(sched)},
            "out": str(Path(tmp) / "zoo"),
            "models": [model],
            "h_max_ns": int(h_max_ns),
            "write_fills": False,
            "write_trajectory": True,
            "bootstrap_b": 1,
        }
        excl = [{"instrument_id": i, "order_id": o} for i, o in excluded]
        try:
            _native("run_zoo", _validate(s.ZooSpec, {**base, "exclude_order_ids": excl}), m.ZooSummary)
        except QtError as e:
            if not excl or "exclude_order_ids" not in e.message:
                raise
            notes.append("model: this core cannot remove your own order from the L2 view; the model saw it")
            _native("run_zoo", _validate(s.ZooSpec, base), m.ZooSummary)
        p = Path(tmp) / "zoo" / "zoo_trajectory.parquet"
        return pq.read_table(p) if p.exists() else None


def _merge_model(x: WhyExplanation, rows: pa.Table, tol: float) -> None:
    """Fills `model_queue_ahead` (estimate as of each event's time) and `first_divergence` in place."""
    ts = rows.column("ts_event").to_pylist()
    est = rows.column("queue_ahead_est").to_pylist()
    order = sorted(range(len(ts)), key=lambda i: (ts[i], i))
    ts = [int(ts[i]) for i in order]
    est = [est[i] for i in order]
    import bisect

    vals: list[float | None] = []
    for e in x.trace.events:
        j = bisect.bisect_right(ts, int(e.ts_event)) - 1
        v = est[j] if j >= 0 else None
        vals.append(float(v) if v is not None and v == v else None)
    x.model_queue_ahead = vals
    x.divergence_tol = tol
    x.first_divergence = None
    for b in _batches(x.trace.events):
        i = b[-1]
        v = vals[i]
        if v is not None and abs(v - float(x.trace.events[i].queue_ahead_after)) >= tol:
            x.first_divergence = i
            break


def _annotate(x: WhyExplanation) -> m.WhyTrace:
    """The trace with the model column in each event's note (`model_ahead=<x>`) and the divergence marker."""
    if x.model_id is None or not x.model_queue_ahead:
        return x.trace
    events = []
    for i, e in enumerate(x.trace.events):
        parts = [e.note] if e.note else []
        v = x.model_queue_ahead[i]
        if v is not None:
            parts.append(f"model_ahead={v:.6g}")
        if i == x.first_divergence:
            parts.append(f"first_divergence: {x.model_id} {v:.6g} vs exact {e.queue_ahead_after}")
        events.append(e.model_copy(update={"note": "; ".join(parts) if parts else None}))
    return x.trace.model_copy(update={"events": events})


def _own_twin(root: Path, live_dir: str, leg: pa.Table, order_key: str, notes: list[str]) -> tuple[pa.Table, list]:
    """Self-exclusion for a live order on MBO data (ruling A1), as the scorer's exact arm does it: the order's MBO
    twin (by venue order id, else side/price/size/time), and the leg moved 1 ns earlier when the twin's add opens
    its timestamp, so the shadow takes the twin's queue place. Returns (leg, [(instrument_id, order_id)])."""
    from queuetruth.score.selfexclude import align_to_twins, match_twins, twin_entry_points

    try:
        from queuetruth import live as qlive

        orders, _ = qlive.read_log(live_dir)
        vid = None
        for r in orders.select(["order_key", "venue_order_id"]).to_pylist():
            if r["order_key"] == order_key:
                vid = r["venue_order_id"]
                break
        r0 = leg.slice(0, 1)
        ts_submit = int(pc.cast(r0.column("ts_submit"), pa.int64())[0].as_py())
        row = {
            "order_key": order_key,
            "shadow_id": str(r0.column("shadow_id")[0].as_py()),
            "instrument_id": int(r0.column("instrument_id")[0].as_py()),
            "side": int(r0.column("side")[0].as_py()),
            "price": int(r0.column("price")[0].as_py()),
            "qty": int(r0.column("qty")[0].as_py()),
            "ts_entry": ts_submit,
            "ts_submit": ts_submit,
            "latency_ns": int(r0.column("latency_ns")[0].as_py() or 0),
            "venue_order_id": vid,
        }
        tw = match_twins(root, [row])
    except (QtError, OSError, KeyError, ValueError, TypeError, pa.ArrowException) as e:
        notes.append(f"self-exclusion: twin lookup failed ({e}); the trace runs on the full tape")
        return leg, []
    if not tw.ids:
        notes.append(f"self-exclusion: no MBO record of {order_key} found; the trace runs on the full tape")
        return leg, []
    if tw.by_heuristic:
        notes.append(f"self-exclusion: {order_key} matched to its MBO twin by side/price/size/time, not by id")
    pts = twin_entry_points(root, tw.ids)
    moved, n = align_to_twins([row], tw, pts)
    if n:
        f = leg.schema.field("ts_submit")
        arr = pa.array([int(moved[0]["ts_submit"])], pa.int64()).cast(f.type)
        leg = leg.set_column(leg.schema.get_field_index("ts_submit"), f, arr)
    return leg, [(i, o) for i, ids in sorted(tw.ids.items()) for o in sorted(ids)]


def _shadow_leg(path: str, shadow_id: str) -> pa.Table:
    t = pq.read_table(path)
    t = t.filter(pc.equal(t.column("shadow_id"), shadow_id))
    if t.num_rows == 0:
        raise QtError("QT-CONFIG-001", f"shadow_id: {shadow_id!r} not found in {path}")
    return t.slice(0, 1)


def why_explain(
    spec: s.WhySpec | dict[str, Any],
    *,
    model: str | None = None,
    zoo: str | None = None,
    divergence_tol: float | None = None,
) -> WhyExplanation:
    """`why` with its explanation: the exact trace of one shadow (`shadows` + `shadow_id`) or one live order (`live`
    + `order_key`: its first schedule leg, entry = exchange ack, with the order's own MBO records removed from the
    tape, ruling A1), the zoo model's queue estimate per event, the first event where model and exact queue
    diverge, and a one-sentence conclusion.

    The model comes from `zoo` (an output directory with `zoo_trajectory.parquet` holding the shadow; `model`
    selects one of its models, default the one furthest from the exact queue) or, with `model` alone or when the
    directory lacks the shadow, from a one-shadow zoo run. `divergence_tol` (default: 10% of the order's size, at
    least one size unit) is the gap that counts as divergence. An L2-only dataset has no exact queue: QT-CONFIG-001
    with a command that works there in the hint. Bare order ids in a spec dict's `exclude_order_ids` apply to every
    instrument of the dataset (`resolve_order_ids`, as the CLI)."""
    sp = _validate(s.WhySpec, _expand_order_ids(spec))
    root = Path(_path_of(sp.dataset, "dataset"))
    shadows_path = _path_of(sp.shadows, "shadows") if sp.shadows is not None else None
    live_dir = _path_of(sp.live, "live") if sp.live is not None else None
    if dataset_kind(root) == "l2":
        hint = why_suggestion(
            str(root), shadows=shadows_path, shadow_id=sp.shadow_id, live=live_dir, order_key=sp.order_key, model=model
        )
        raise QtError(
            "QT-CONFIG-001",
            f"why: {root} is an L2-only dataset; an exact queue trace needs MBO data",
            f"see the model's queue estimate instead: {hint}" if hint else None,
        )
    notes: list[str] = []
    excluded: list[tuple[int, int]] = []
    with tempfile.TemporaryDirectory(prefix="qt-why-") as tmp:
        if live_dir is None:
            if shadows_path is None or sp.shadow_id is None:
                raise QtError("QT-CONFIG-001", "why: shadows and shadow_id (or live and order_key) are required")
            leg = None
            run = sp.model_copy(update={"out": None})
        else:
            if sp.order_key is None:
                raise QtError("QT-CONFIG-001", "order_key: required with live", "--order-key KEY")
            leg = _live_leg(live_dir, sp.order_key)
            if dataset_kind(root) == "mbo":
                leg, excluded = _own_twin(root, live_dir, leg, sp.order_key, notes)
            sched = Path(tmp) / "schedule.parquet"
            write_table(leg, sched, SCHEMAS["qt.shadow_spec"])
            sid = leg.column("shadow_id")[0].as_py()
            # `live` + `order_key` stay in the spec: the core removes the order's own MBO records (A1).
            run = sp.model_copy(update={"shadows": s.PathRef(path=str(sched)), "shadow_id": sid, "out": None})
        trace = _native("why", run, m.WhyTrace)
        units = _instrument_units(root, trace.instrument_id, trace.ts_live)
        x = WhyExplanation(trace=trace, excluded_order_ids=[o for _, o in excluded], notes=notes, **units)
        if model is not None or zoo is not None:
            sid = trace.subject.shadow_id or sp.shadow_id or ""
            tol = divergence_tol if divergence_tol is not None else max(1.0, 0.1 * float(trace.qty))
            rows = None
            if zoo is not None:
                tp = Path(zoo) / "zoo_trajectory.parquet"
                stale = _zoo_ts_live(Path(zoo), sid)
                if tp.exists() and stale is not None and trace.ts_live is not None and stale != trace.ts_live:
                    notes.append(
                        f"model: {zoo} ran {sid} with another schedule (went live at {fmt_utc(stale)}, here at "
                        f"{fmt_utc(trace.ts_live)}); its trajectory is not used"
                        + ("; running the model on this order" if model else "")
                    )
                elif tp.exists():
                    mid, rows = _pick_model(_traj_rows(pq.read_table(tp), sid), model)
                    bad = _exact_mismatches(trace, rows) if rows.num_rows else 0
                    if bad:
                        notes.append(
                            f"model: the exact queue in {tp} disagrees with this trace at {bad} point(s) (another "
                            "dataset, schedule or self-exclusion); its trajectory is not used"
                            + ("; running the model on this order" if model else "")
                        )
                        rows = None
                    elif rows.num_rows == 0:
                        notes.append(
                            f"model: {tp} has no trajectory rows for {sid}"
                            + (f" and model {model}" if model else "")
                            + ("; running the model on this order" if model else "")
                        )
                        rows = None
                    else:
                        x.model_id = mid
                else:
                    notes.append(
                        f"model: {tp} not found (run zoo with --write-trajectory)"
                        + ("; running the model on this order" if model else "")
                    )
            if rows is None and model is not None:
                if leg is None:
                    leg = _shadow_leg(shadows_path or "", sp.shadow_id or "")
                last = max((e.ts_event for e in trace.events), default=trace.ts_live or 0)
                span = max(0, last - (trace.ts_live or last))
                traj = _model_run(root, leg, model, excluded, max(300 * _NS, span + _NS), notes)
                if traj is not None and traj.num_rows:
                    mid, rows = _pick_model(_traj_rows(traj, str(leg.column("shadow_id")[0].as_py())), None)
                    x.model_id = mid
                else:
                    notes.append(f"model: {model} produced no queue estimate for this order")
            if rows is not None and x.model_id is not None:
                _merge_model(x, rows, tol)
            elif model is not None and x.model_id is None:
                x.model_id = model
    x.notes = list(notes)  # validation copied the list; later notes are added here
    x.conclusion = _conclusion(x)
    x.trace = _annotate(x)
    if sp.out:
        _write_json(x.trace.model_dump(mode="json"), Path(sp.out))
    return x


def why(
    spec: s.WhySpec | dict[str, Any],
    *,
    model: str | None = None,
    zoo: str | None = None,
    divergence_tol: float | None = None,
) -> m.WhyTrace:
    """`why`: event trace of one shadow (`shadows` + `shadow_id`) or of one live order (`live` + `order_key`: its
    first schedule leg, entry = exchange ack, own MBO records removed). With `model` / `zoo` the events' notes carry
    the model's queue estimate and the first divergence (see `why_explain`, which also returns them as data and a
    conclusion). Caveats are emitted as warnings."""
    x = why_explain(spec, model=model, zoo=zoo, divergence_tol=divergence_tol)
    for n in x.notes:
        warnings.warn(n, UserWarning, stacklevel=2)
    return x.trace


def export(spec: s.ExportSpec | dict[str, Any], *, force: bool = False) -> m.ExportSummary:
    """`export`: `l2_npz` in Rust, the snippets and the latency npz in `queuetruth.export.run_export`. `force` (or
    the spec's `force`) exports a calibration that `run_export` refuses (fitted on nothing, degenerate, or worse than
    the default on held-out data); it is passed straight through."""
    sp = _validate(s.ExportSpec, spec)
    if sp.target == "l2_npz":
        return _native("export_l2_npz", sp, m.ExportSummary)
    from queuetruth.export import run_export

    return run_export(sp, force=force)


#: `queuetruth.export` functions tried, in order, for `export replay-schedule` (the first that exists is called).
REPLAY_SCHEDULE_FUNCTIONS = ("replay_schedule", "export_replay_schedule")


def export_replay_schedule(
    target: str,
    *,
    out: str,
    schedule: str | None = None,
    amends: str | None = None,
    live: str | None = None,
    dataset: str | None = None,
    entry: str | None = None,
) -> m.ExportSummary:
    """`export replay-schedule`: a NautilusTrader strategy or an hftbacktest script that replays a live order schedule
    (`schedule` + optional `amends` tables and/or the live-log directory `live`; `dataset` supplies tick and lot
    sizes), so the simulator's fills can be imported (`live import --venue sim-*`) and scored with `score --sim-log`.
    Delegates to `queuetruth.export.replay_schedule(target, out, *, schedule, amends, live, dataset, entry)` (only
    the arguments given are passed); a build without it raises QT-USAGE-001. The adapter's notes are emitted as
    warnings."""
    import dataclasses
    import inspect

    import queuetruth.export as qexp

    target = target.replace("-", "_")
    if target not in ("hftbacktest", "nautilus"):
        raise QtError("QT-CONFIG-001", f"target: expected hftbacktest or nautilus, got {target!r}")
    if schedule is None and live is None:
        raise QtError(
            "QT-CONFIG-001", "export replay-schedule: a schedule or a live log is required", "--schedule FILE"
        )
    fn = next((getattr(qexp, n) for n in REPLAY_SCHEDULE_FUNCTIONS if callable(getattr(qexp, n, None))), None)
    if fn is None:
        raise QtError(
            "QT-USAGE-001",
            "export replay-schedule: this queuetruth build has no schedule-replay adapters "
            "(queuetruth.export.replay_schedule)",
            "upgrade queuetruth",
        )
    given = {"schedule": schedule, "amends": amends, "live": live, "dataset": dataset}
    if entry is not None:
        given["entry"] = entry.replace("-", "_")
    params = inspect.signature(fn).parameters
    if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        unknown = sorted(k for k, v in given.items() if v is not None and k not in params)
        if unknown:
            raise QtError("QT-CONFIG-001", f"export replay-schedule: this build does not take {', '.join(unknown)}")
    res = fn(target=target, out=out, **{k: v for k, v in given.items() if v is not None})
    if isinstance(res, m.ExportSummary):
        return res
    info: dict[str, Any] = {}
    if dataclasses.is_dataclass(res) and not isinstance(res, type):
        info = dataclasses.asdict(res)
    elif isinstance(res, BaseModel):
        info = res.model_dump(mode="json")
    elif isinstance(res, dict):
        info = dict(res)
    for note in info.get("notes") or []:
        warnings.warn(str(note), UserWarning, stacklevel=2)
    from queuetruth.schemas.io import sha256_file

    p = Path(str(info.get("path") or out))
    files = [m.FileEntry(name=p.name, bytes=p.stat().st_size, sha256=sha256_file(p))] if p.is_file() else []
    text = res if isinstance(res, str) and not Path(res).exists() else None
    return m.ExportSummary(target=target, files=files, text=text)  # type: ignore[arg-type]


def fill_liquidity(fills: str | pa.Table) -> str | pa.Table:
    """Liquidity of shadow fills (audit A5): for a `qt.shadow_fills` reason name, `maker` or `taker`; for a table
    with a `reason` column, the table with a `liquidity` column appended (unchanged when it already has one, as
    schema-1.1 `qt.shadow_fills` do)."""
    if isinstance(fills, str):
        return str(_call("fill_liquidity", fills))
    if "liquidity" in fills.column_names:  # schema 1.1 writes the column itself
        return fills
    reasons = fills.column("reason").to_pylist()
    names = {r: str(_call("fill_liquidity", r)) for r in set(reasons) if r is not None}
    return fills.append_column("liquidity", pa.array([names.get(r) for r in reasons], pa.utf8()))


def read_table(path: str, expect: str | None = None) -> pa.Table:
    """Reads a Parquet table and checks its `qt.schema` metadata (and the columns, for tables known to the core)."""
    p = Path(path)
    if not p.exists():
        raise QtError("QT-INPUT-001", f"{p}: not found")
    try:
        meta = pq.read_schema(p).metadata or {}
    except (OSError, pa.ArrowInvalid) as e:
        raise QtError("QT-INPUT-002", f"{p}: not a Parquet file: {e}") from None
    name = meta.get(b"qt.schema", b"").decode()
    if not name:
        raise QtError("QT-INPUT-002", f"{p}: missing qt.schema metadata (not a queuetruth table)")
    if expect is not None and name != expect:
        raise QtError("QT-INPUT-002", f"{p}: expected table {expect}, found {name}")
    core = _core()
    if name in core.schema_names():
        return _call("read_table", str(p), expect)
    return pq.read_table(p)


class Session:
    """Interactive replay session: `.advance_to(ts)`, `.bbo(inst)`, `.depth(inst, n)`, `.queue_ahead(...)`.

    `bbo` returns `{"bid": (price, qty, count) | None, "ask": ...}` (None before the instrument's first record);
    `depth` returns `(bids, asks)` lists of `(price, qty, count)`, best first; `queue_ahead(inst, side, px)` returns
    the displayed `(qty, count)` at that price (side `"B"` or `"A"`)."""

    def __init__(self, dataset: str, venue: str | None = None) -> None:
        core = _core()
        try:
            self._s = core.Session(str(dataset), venue)
        except core.QtCoreError as e:
            raise _qt_error(e) from None

    def _wrap(self, fn: Callable[..., Any], *args: Any) -> Any:
        core = _core()
        try:
            return fn(*args)
        except core.QtCoreError as e:
            raise _qt_error(e) from None

    def advance_to(self, ts: int) -> None:
        """Replays every record with `ts_event <= ts` (GIL released)."""
        self._wrap(self._s.advance_to, int(ts))

    def bbo(self, instrument_id: int) -> dict[str, tuple[int, int, int] | None] | None:
        """Best bid and ask."""
        return self._wrap(self._s.bbo, int(instrument_id))

    def depth(self, instrument_id: int, n: int) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
        """Top `n` levels per side."""
        return self._wrap(self._s.depth, int(instrument_id), int(n))

    def queue_ahead(self, instrument_id: int, side: str, price: int) -> tuple[int, int] | None:
        """Displayed quantity and order count at a price."""
        return self._wrap(self._s.queue_ahead, int(instrument_id), str(side), int(price))


__all__ = [
    "LATENCY_FIT_KEYS",
    "REPLAY_SCHEDULE_FUNCTIONS",
    "Session",
    "WhyExplanation",
    "calibrate",
    "calibrate_live",
    "dataset_kind",
    "dq",
    "export",
    "export_replay_schedule",
    "extract_features",
    "fill_liquidity",
    "fit_hazard",
    "fixtures_synth",
    "import_live",
    "ingest",
    "mirror_real_orders",
    "read_table",
    "referee",
    "referee_fixtures",
    "replay",
    "report",
    "resolve_order_ids",
    "run_id",
    "run_zoo",
    "sample_shadows",
    "score",
    "shadows_from_live",
    "validate_live",
    "why",
    "why_explain",
    "why_suggestion",
]
