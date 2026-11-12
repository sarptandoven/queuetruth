"""`calibrate` and `calibrate-live`.

`calibrate` (alloc, survival, nt_touch): the Rust fit (`fit.json`), then the fitted and the default-parameter model on
the test window through the zoo (`held_out/`), `evaluate_zoo` -> `held_out` / `baseline_held_out`, and the config
exports -> `calibration.json`. `calibrate` (latency): `queuetruth.live.latency_stats` (restricted to the `LatencyFit`
fields), the latency npz and the exports.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from queuetruth.dataset import open_dataset
from queuetruth.errors import QtError
from queuetruth.ledger import recorded as _ledger_recorded
from queuetruth.ops._call import _dump, _native, _out_dir, _path_of, _resolve_ref, _validate, _write_json
from queuetruth.ops.zoo import _evaluate
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as s
from queuetruth.schemas.arrow import ENGINE_VERSION
from queuetruth.schemas.runid import run_id as _run_id
from queuetruth.schemas.runid import spec_echo

#: Fields of `qt.calibration_result.latency` (`queuetruth.live.latency_stats` returns more).
LATENCY_FIT_KEYS = ("entry_ns", "response_ns", "cancel_ns", "amend_ns", "clock_sync_assumed")


def _model_ids(family: str, estimate: float) -> tuple[str, str]:
    from queuetruth.score.calibrate_live import DEFAULTS, model_id

    return model_id(family, estimate), model_id(family, DEFAULTS[family])


def _instrument_index(dataset: str) -> dict[tuple[int, int], tuple[int, int, int]]:
    """(instrument_id, session_date days) -> (midnight_utc_ns, open_s, close_s)."""
    out = {}
    for ses in open_dataset(dataset, missing_ok=True).sessions:
        out[ses.key] = (int(ses.midnight_utc_ns), int(ses.open_s), int(ses.close_s))  # type: ignore[arg-type]
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


@_ledger_recorded("calibrate")
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


@_ledger_recorded("calibrate-live")
def calibrate_live(spec: Any, **kwargs: Any) -> m.CalibrationResult:
    """`calibrate-live`: fit a zoo parameter to the user's own live fills (`queuetruth.score.calibrate_live`)."""
    from queuetruth.score import calibrate_live as _calibrate_live

    return _calibrate_live(spec, **kwargs)
