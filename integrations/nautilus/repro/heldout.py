#!/usr/bin/env python3
"""Held-out check inside NautilusTrader: a queuetruth calibration fitted on one day, scored on another day.

The calibrated `FillModel` comes from `queuetruth_nautilus` (built from the `qt.calibration_result` file, not typed
in), the L2 tape, orders and strategy come unchanged from `scripts/nt_crosscheck/run.py`, and every order's fill is
scored against queuetruth's exact replay of the held-out day. Three steps, each in the interpreter that has its
dependencies:

  fit      (queuetruth venv)    optional: refits the calibration with the current queuetruth build, from the spec
                                echoed in an existing calibration (same family, windows, grid, bootstrap) with the
                                dataset and shadows given here; writes <out>/calibration.json.
  prepare  (queuetruth venv)    keeps the shadows submitted on the calibration's test dates (session-local date of
                                `ts_submit`), then runs the harness's `prepare` on the test day: L2 tape, faithful zoo
                                (whose rows carry the exact arm's `truth_filled`), instruments, resolved prices.
  nautilus (NautilusTrader venv with queuetruth-nautilus installed)
                                one harness run per configuration and seed:
                                  nt_default     NautilusTrader's default fill model (`prob_fill_on_limit = 1`,
                                                 constructed with only the harness's random_seed) on the same venue
                                                 setup (`queue_position` as the calibrated family): the fit's
                                                 baseline; one seed (deterministic at p = 1)
                                  qt_calibrated  `queuetruth_nautilus.fill_model(calibration, random_seed=...)` and
                                                 `queue_position` from `queuetruth_nautilus.venue_options`; `--seeds`
                                                 seeds (NautilusTrader draws each touch fill at random)
                                The harness constructs the fill model by name at run time; this step swaps that name for
                                a factory that returns the objects above and checks that the harness asked for the same
                                probability, so the engine runs exactly what the package builds.
  score    (any venv with pyarrow, pandas, numpy)
                                fill rate and bias against exact with an order-level bootstrap 95% CI; paired tests
                                (orders resampled jointly) of the |bias| reduction, default minus calibrated, for the
                                seed-averaged model and for one run, and of the order-level Brier gain; the default's
                                order-by-order agreement with the zoo's `nt_prob_touch[p=1]`; the seed-averaged
                                calibrated fills against the zoo's per-order prediction for the fitted model (from the
                                calibration's held_out/zoo_predictions.parquet); the calibration's own held-out
                                numbers side by side.

Usage (from the repository root; NT_PY is a NautilusTrader venv with this package installed):

  .venv/bin/python integrations/nautilus/repro/heldout.py fit --spec-from OLD.json \
      --dataset .realdata/ds/nasdaq-2d --shadows .realdata/shadows/2d-sampled-6000.parquet --out FITDIR
  .venv/bin/python integrations/nautilus/repro/heldout.py prepare --calibration CAL.json \
      --dataset .realdata/ds/nasdaq-2020-01-30 --shadows .realdata/shadows/2d-sampled-6000.parquet --work W
  $NT_PY integrations/nautilus/repro/heldout.py nautilus --calibration CAL.json --work W --seeds 8 --procs 2
  $NT_PY integrations/nautilus/repro/heldout.py score --calibration CAL.json --work W

NautilusTrader is LGPL-3.0: this script only calls its public Python API.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib.util
import json
import re
import sys
import time
import warnings
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve()
ROOT = HERE.parents[3]
HARNESS = ROOT / "scripts" / "nt_crosscheck" / "run.py"
DEFAULT = "nt_default"
CALIBRATED = "qt_calibrated"
#: base of the harness `seed` argument per seed index (the harness adds 7919 * chunk + instrument id)
SEED_STRIDE = 1_000_003
BOOTSTRAP_B = 2000


def load_harness(path: Path = HARNESS) -> ModuleType:
    """`scripts/nt_crosscheck/run.py` as a module (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("qt_nt_crosscheck", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load the NautilusTrader harness from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["qt_nt_crosscheck"] = mod
    spec.loader.exec_module(mod)
    return mod


def _calibration_doc(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------------------------
# prepare (queuetruth venv)
# ---------------------------------------------------------------------------------------------------------------


def test_dates(doc: dict[str, Any]) -> list[str]:
    """The calibration's held-out dates; a block-only test window has none and is refused here."""
    dates = (doc.get("test") or {}).get("dates")
    if not dates:
        raise SystemExit("the calibration's test window has no dates (blocks only); this check needs a held-out day")
    return [str(d) for d in dates]


def cmd_fit(a: argparse.Namespace) -> None:
    import queuetruth as qt

    spec = dict(_calibration_doc(a.spec_from)["spec"])
    spec["dataset"] = {"path": str(a.dataset)}
    spec["shadows"] = {"path": str(a.shadows)}
    spec["out"] = str(a.out)
    spec["threads"] = a.threads
    t0 = time.time()
    res = qt.calibrate(spec)
    doc = json.loads(res.model_dump_json(by_alias=True))
    out = Path(a.out) / "calibration.json"
    if not out.exists():
        out.write_text(json.dumps(doc, indent=1))
    print(
        f"[fit] {doc['family']} = {doc['estimate']!r} CI {doc['ci_bootstrap']} on {doc['n_events']} events; trust "
        f"{(doc.get('trust') or {}).get('verdict')}; {time.time() - t0:.0f}s -> {out}",
        flush=True,
    )


def cmd_prepare(a: argparse.Namespace) -> None:
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    doc = _calibration_doc(a.calibration)
    dates = set(test_dates(doc))
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    sh = pq.read_table(a.shadows)
    ts = sh["ts_submit"]
    if ts.type.tz is None:
        ts = pc.assume_timezone(ts, "UTC")
    local = pc.strftime(pc.cast(ts, pa.timestamp("ns", tz=a.tz)), format="%Y-%m-%d")
    keep = pc.is_in(local, pa.array(sorted(dates)))
    sub = sh.filter(keep)
    if sub.num_rows == 0:
        raise SystemExit(f"no shadow in {a.shadows} was submitted on {sorted(dates)} ({a.tz})")
    out = work / "shadows_test.parquet"
    pq.write_table(sub, out)
    print(f"[prepare] {sub.num_rows} of {sh.num_rows} shadows on {sorted(dates)} -> {out}", flush=True)
    h = load_harness(Path(a.harness))
    h.cmd_prepare(
        argparse.Namespace(work=str(work), dataset=a.dataset, shadows=str(out), n_shadows=0, threads=a.threads)
    )
    (work / "heldout_prepare.json").write_text(
        json.dumps(
            {
                "calibration_run_id": doc.get("run_id"),
                "test_dates": sorted(dates),
                "dataset": str(a.dataset),
                "shadows": str(a.shadows),
                "n_shadows": sub.num_rows,
            },
            indent=1,
        )
    )


# ---------------------------------------------------------------------------------------------------------------
# nautilus (NautilusTrader venv)
# ---------------------------------------------------------------------------------------------------------------


@contextlib.contextmanager
def harness_fill_model(config: str, cal: Any, p_expected: float, force: bool = False) -> Iterator[list[Any]]:
    """Swaps the name the harness constructs its fill model from for a factory returning the package's object.

    The harness imports `nautilus_trader.backtest.models.FillModel` (1.x) or `nautilus_trader.execution.
    DefaultFillModel` (2.x) inside its run function, so replacing that module attribute for the duration of a run is
    enough. Yields the list of objects the factory built (for provenance)."""
    import queuetruth_nautilus as qtn
    from queuetruth_nautilus import compat

    mod_name, attr = compat.fill_model_path()
    mod = importlib.import_module(mod_name)
    original = getattr(mod, attr)
    built: list[Any] = []

    def factory(prob_fill_on_limit: float = 1.0, prob_slippage: float = 0.0, random_seed: int | None = None) -> Any:
        if prob_fill_on_limit != p_expected:  # exact: the harness passes the value it was given
            raise AssertionError(f"harness asked for p={prob_fill_on_limit!r}, expected {p_expected!r}")
        if prob_slippage != 0.0:
            raise AssertionError(f"harness asked for prob_slippage={prob_slippage!r}")
        if config == DEFAULT:
            obj = original() if random_seed is None else original(random_seed=random_seed)
        else:
            setattr(mod, attr, original)  # the package resolves the same name: build with the real class
            try:
                obj = qtn.fill_model(cal, random_seed=random_seed, force=force)
            finally:
                setattr(mod, attr, factory)
            if type(obj) is not original:
                raise AssertionError(f"package built {type(obj)!r}, the harness expects {original!r}")
        built.append(obj)
        return obj

    setattr(mod, attr, factory)
    try:
        yield built
    finally:
        setattr(mod, attr, original)


def _nt_job(job: tuple) -> tuple[list[dict], dict]:
    harness_path, cal_path, work, iid, config, seed_index, window_ns, chunk, force = job
    warnings.filterwarnings("ignore")
    import nautilus_trader

    import queuetruth_nautilus as qtn
    from queuetruth_nautilus.compat import describe as compat_describe

    h = load_harness(Path(harness_path))
    cal = qtn.load_calibration(cal_path)
    opts = qtn.venue_options(cal, force=force)
    p = 1.0 if config == DEFAULT else float(cal.prob_fill_on_limit)
    qpos = bool(opts["queue_position"])  # the default is the same venue setup at p = 1 (the fit's baseline)
    h.CONFIGS[config] = (qpos, p, False)
    base_seed = 42 + SEED_STRIDE * seed_index
    t0 = time.time()
    with harness_fill_model(config, cal, p, force) as built:
        rows = h.run_instrument_nt(Path(work), int(iid), [config], int(window_ns), int(chunk), base_seed)
    for r in rows:
        r["seed_index"] = seed_index
    info = {
        "instrument_id": int(iid),
        "config": config,
        "seed_index": seed_index,
        "n_engine_runs": len(built),
        "fill_model": compat_describe(built[0]) if built else None,
        "queue_position": qpos,
        "seconds": round(time.time() - t0, 1),
        "nautilus_trader": nautilus_trader.__version__,
    }
    return rows, info


def cmd_nautilus(a: argparse.Namespace) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    warnings.filterwarnings("ignore")
    import nautilus_trader

    import queuetruth_nautilus as qtn

    cal = qtn.load_calibration(a.calibration)
    qtn.check_trust(cal, force=a.force)
    if cal.prob_fill_on_limit is None:
        raise SystemExit(f"{a.calibration}: no fitted prob_fill_on_limit ({cal.family})")
    work = Path(a.work)
    iids = sorted({int(x) for x in pq.read_table(work / "shadows.parquet")["instrument_id"].to_pylist()})
    jobs = [(str(a.harness), a.calibration, str(work), iid, DEFAULT, 0, a.window_ns, a.chunk, a.force) for iid in iids]
    jobs += [
        (str(a.harness), a.calibration, str(work), iid, CALIBRATED, s, a.window_ns, a.chunk, a.force)
        for s in range(a.seeds)
        for iid in iids
    ]
    rows: list[dict] = []
    infos: list[dict] = []
    t0 = time.time()
    if a.procs > 1:
        import multiprocessing as mp

        with mp.get_context("spawn").Pool(a.procs) as pool:
            for r, info in pool.imap_unordered(_nt_job, jobs):
                rows.extend(r)
                infos.append(info)
                print(f"[nautilus] {info}", flush=True)
    else:
        for j in jobs:
            r, info = _nt_job(j)
            rows.extend(r)
            infos.append(info)
            print(f"[nautilus] {info}", flush=True)
    ver = nautilus_trader.__version__
    out = work / (f"nt-{ver}-{a.tag}" if a.tag else f"nt-{ver}")
    out.mkdir(exist_ok=True)
    rows.sort(key=lambda r: (r["config"], r["seed_index"], r["shadow_id"]))
    t = pa.Table.from_pylist(rows).replace_schema_metadata({"nautilus_trader": ver})
    pq.write_table(t, out / "heldout_outcomes.parquet")
    infos.sort(key=lambda i: (i["config"], i["seed_index"], i["instrument_id"]))
    (out / "heldout_runs.json").write_text(
        json.dumps(
            {
                "nautilus_trader": ver,
                "queuetruth_nautilus": qtn.__version__,
                "python": sys.version.split()[0],
                "calibration": cal.summary(),
                "seeds": a.seeds,
                "seconds": round(time.time() - t0, 1),
                "runs": infos,
            },
            indent=1,
        )
    )
    print(f"[nautilus] wrote {len(rows)} rows to {out} ({time.time() - t0:.0f}s)", flush=True)


# ---------------------------------------------------------------------------------------------------------------
# score (pyarrow + pandas + numpy)
# ---------------------------------------------------------------------------------------------------------------


def _weights(n: int, b: int = BOOTSTRAP_B, seed: int = 0) -> Any:
    """(b, n) resampling counts: row k counts how often each order is drawn in bootstrap replicate k. Every
    statistic of one horizon uses the same rows, so paired differences resample orders jointly."""
    import numpy as np

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(b, n))
    return np.bincount((np.arange(b)[:, None] * n + idx).ravel(), minlength=b * n).reshape(b, n)


def _ci(reps: Any) -> list[float]:
    """Percentile 95% interval of bootstrap replicates."""
    import numpy as np

    return [float(np.quantile(reps, 0.025)), float(np.quantile(reps, 0.975))]


def _stat(point: float, reps: Any, scale: float = 100.0) -> dict[str, Any]:
    """`{"value", "ci"}` (scaled, e.g. to pp) of a statistic and its bootstrap replicates."""
    lo, hi = _ci(reps)
    return {"value": scale * point, "ci": [scale * lo, scale * hi]}


_FILL_PARAM = re.compile(r"(prob_fill_on_limit|prob_slippage)[=:] ?([0-9.eE+-]+)")


def fill_model_params(text: str | None) -> dict[str, Any] | None:
    """Class and fitted parameters from a recorded fill-model description (1.x `FillModel(p=...)` or the 2.x repr,
    whose seed and RNG state are per run and left out)."""
    if not text:
        return None
    out: dict[str, Any] = {"class": re.split(r"[ ({]", text, maxsplit=1)[0]}
    for k, v in _FILL_PARAM.findall(text):
        out[k] = float(v)
    return out


def _zoo_fitted(path: Path | None, model_id: str | None, ids: list[str], horizons: list[int]) -> Any:
    """The zoo's per-order expected fill probability of the fitted model (`p_fill`, `1 - (1 - p)^touches` for the
    touch model) from the calibration's held-out predictions, or None when the file or the model is missing."""
    import pyarrow.parquet as pq

    if path is None or model_id is None or not path.exists():
        return None
    zp = pq.read_table(path, columns=["model_id", "shadow_id", "horizon_ns", "p_fill"]).to_pandas()
    zp = zp[(zp.model_id == model_id) & zp.horizon_ns.isin(horizons)]
    if zp.empty:
        return None
    return zp.pivot(index="shadow_id", columns="horizon_ns", values="p_fill").reindex(ids).fillna(0.0)


def score(
    work: Path,
    run: str,
    doc: dict[str, Any],
    window_ns: int = 60_000_000_000,
    zoo_held_out: Path | None = None,
) -> dict[str, Any]:
    """The held-out report (JSON-able) of `<work>/<run>/heldout_outcomes.parquet` (`run` = `nt-<version>[-<tag>]`).

    `zoo_held_out` is the calibration's own held-out `zoo_predictions.parquet` (written next to its
    calibration.json); with it the report also checks NautilusTrader's seed-averaged fills against the zoo's
    per-order prediction for the fitted model."""
    import numpy as np
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    h = load_harness()
    horizons = list(h.HORIZONS_NS)
    hname = dict(h.HNAME)
    sh = pq.read_table(work / "shadows.parquet").to_pandas().set_index("shadow_id")
    ids = sorted(sh.index)
    n = len(ids)
    path = work / run / "heldout_outcomes.parquet"
    meta = pq.read_schema(path).metadata or {}
    ver = meta[b"nautilus_trader"].decode() if b"nautilus_trader" in meta else run.removeprefix("nt-")
    # nullable Int64, never float64: at epoch nanoseconds float64 is 256 ns coarse, enough to move a fill across a
    # horizon boundary
    out = pq.read_table(path).to_pandas(types_mapper={pa.int64(): pd.Int64Dtype()}.get)
    zp = pq.read_table(work / "zoo" / "zoo_predictions.parquet").to_pandas()
    zp = zp[zp.horizon_ns.isin(horizons)]
    p1_model = h.PAIRS["touch_p1"]
    truth = zp[zp.model_id == p1_model].pivot(index="shadow_id", columns="horizon_ns", values="truth_filled")
    truth = truth.reindex(ids).astype(float).fillna(0.0)
    # the zoo's faithful replica of the default (the calibration's baseline model when the zoo ran it)
    base_id = next(iter(doc.get("baseline_held_out") or {}), p1_model)
    if base_id not in set(zp.model_id):
        base_id = p1_model
    zoo_p1 = zp[zp.model_id == base_id].pivot(index="shadow_id", columns="horizon_ns", values="p_fill")
    zoo_p1 = zoo_p1.reindex(ids).fillna(0.0)
    fitted_id = next(iter(doc.get("held_out") or {}), None)
    zoo_fit = _zoo_fitted(zoo_held_out, fitted_id, ids, horizons)

    def fill_ts(df: pd.DataFrame) -> tuple[Any, Any, Any]:
        """(has a fill, first fill ts as exact int64 (0 when none), ts_live as int64)."""
        ff = df["first_fill_ts"]
        has = ff.notna().to_numpy(bool)
        return has, ff.fillna(0).astype("int64").to_numpy(), df["ts_live"].astype("int64").to_numpy()

    def filled(df: pd.DataFrame, hz: int) -> pd.DataFrame:
        """shadow x seed fill indicator by ts_live + hz (unsubmitted shadows: 0)."""
        has, ff, tl = fill_ts(df)
        ok = has & (ff <= tl + hz)
        m = df.assign(f=ok.astype(float)).pivot(index="shadow_id", columns="seed_index", values="f")
        return m.reindex(ids).astype(float).fillna(0.0)

    def after_cancel(df: pd.DataFrame) -> int:
        has, ff, tl = fill_ts(df)
        return int((has & (ff > tl + window_ns + 1)).sum())

    dflt = out[out.config == DEFAULT]
    cal = out[out.config == CALIBRATED]
    seeds = sorted(int(x) for x in cal.seed_index.unique())
    zoo_ho = next(iter((doc.get("held_out") or {}).values()), None)
    zoo_base = next(iter((doc.get("baseline_held_out") or {}).values()), None)
    w = _weights(n)
    rep: dict[str, Any] = {
        "nautilus_trader": ver,
        "calibration_run_id": doc.get("run_id"),
        "family": doc.get("family"),
        "p": doc.get("estimate"),
        "train": doc.get("train"),
        "test": doc.get("test"),
        "n_shadows": n,
        "n_submitted": int((sh.price.notna() & ~sh.crosses_at_live).sum()),
        "seeds": len(seeds),
        "bootstrap": {"replicates": BOOTSTRAP_B, "unit": "order", "seed": 0},
        "zoo_default_model": base_id,
        "zoo_fitted_model": fitted_id if zoo_fit is not None else None,
        # Orders whose first fill came after the harness's cancel time (ts_live + window + 1 ns): the harness's
        # cancel is a no-op on nautilus_trader 2.x (see the README), so unfilled orders keep resting. Such a fill
        # is outside every scored horizon and, with queue_position off, does not touch any other order.
        "fills_after_cancel_time": {DEFAULT: after_cancel(dflt), CALIBRATED: after_cancel(cal)},
        "horizons": {},
    }
    for hz in horizons:
        tr = truth[hz].to_numpy()
        d1 = filled(dflt, hz)
        fd = d1[d1.columns[0]].to_numpy()
        fc = filled(cal, hz).to_numpy()  # orders x seeds
        pc_ = fc.mean(axis=1)  # seed-averaged calibrated fill probability per order
        bd = fd - tr
        bc = pc_ - tr
        bs = fc - tr[:, None]  # per-seed (single run) errors
        # bootstrap replicates (rows of w), orders resampled jointly for every statistic
        bd_b = w @ bd / n
        bc_b = w @ bc / n
        bs_b = w @ bs / n  # replicates x seeds
        sq_d = bd**2
        sq_c = bc**2
        sq_s = (bs**2).mean(axis=1)  # one run's squared error, averaged over the seeds
        per_seed = bs.mean(axis=0)
        hzr: dict[str, Any] = {
            "exact_rate": float(tr.mean()),
            "default": {
                "fill_rate": float(fd.mean()),
                "bias_pp": 100 * float(bd.mean()),
                "bias_ci_pp": [100 * x for x in _ci(bd_b)],
                "brier": float(sq_d.mean()),
                "agree_with_zoo_p1": float(((fd > 0.5) == (zoo_p1[hz].to_numpy() > 0.5)).mean()),
            },
            "calibrated": {
                "fill_rate": float(pc_.mean()),
                "bias_pp": 100 * float(bc.mean()),
                "bias_ci_pp": [100 * x for x in _ci(bc_b)],
                "bias_pp_per_seed": {
                    "min": 100 * float(per_seed.min()),
                    "max": 100 * float(per_seed.max()),
                    "sd": 100 * float(per_seed.std(ddof=1)) if len(per_seed) > 1 else None,
                },
                "abs_bias_pp_single_run_mean": 100 * float(np.abs(per_seed).mean()),
                "brier_seed_mean": float(sq_c.mean()),
                "brier_single_seed_mean": float(sq_s.mean()),
            },
            # Paired tests (default minus calibrated; positive = the calibrated model is closer to exact). These can
            # fail: a p far below the fit underfills and its |bias| exceeds the default's.
            "paired": {
                # |aggregate bias| of the default minus that of the seed-averaged calibrated model
                "abs_bias_reduction_pp": _stat(abs(bd.mean()) - abs(bc.mean()), np.abs(bd_b) - np.abs(bc_b)),
                # the same for one calibrated run (|bias| averaged over seeds): what one backtest gets
                "abs_bias_reduction_single_run_pp": _stat(
                    abs(bd.mean()) - float(np.abs(per_seed).mean()), np.abs(bd_b) - np.abs(bs_b).mean(axis=1)
                ),
                # order-level Brier score, default minus calibrated
                "brier_gain_seed_mean": _stat(float((sq_d - sq_c).mean()), w @ (sq_d - sq_c) / n, scale=1.0),
                "brier_gain_single_run": _stat(float((sq_d - sq_s).mean()), w @ (sq_d - sq_s) / n, scale=1.0),
            },
        }
        if zoo_fit is not None:
            z = zoo_fit[hz].to_numpy()
            diff = pc_ - z
            det = (z == 0.0) | (z == 1.0)
            rnd = ~det
            s_ = fc.shape[1]
            # under the per-touch contract each order's seed mean is Binomial(S, z)/S: variance z (1 - z) / S
            disp = float(((diff[rnd] ** 2) / (z[rnd] * (1 - z[rnd]) / s_)).mean()) if rnd.any() else None
            hzr["reproduction"] = {
                "zoo_fitted_rate": float(z.mean()),
                "nt_minus_zoo_pp": _stat(float(diff.mean()), w @ diff / n),
                "deterministic_orders": int(det.sum()),
                "deterministic_mismatches": int((np.abs(diff[det]) > 0).sum()),
                "random_orders": int(rnd.sum()),
                "dispersion_ratio": disp,
            }
        k = str(hz)
        if zoo_ho is not None and zoo_base is not None:
            hzr["zoo_held_out"] = {
                "calibrated_bias_pp": 100 * float(zoo_ho["fill_rate_bias"][k]),
                "default_bias_pp": 100 * float(zoo_base["fill_rate_bias"][k]),
                "calibrated_brier": float(zoo_ho["brier"][k]),
                "default_brier": float(zoo_base["brier"][k]),
            }
        rep["horizons"][hname[hz]] = hzr
    return rep


def _window(w: Any) -> str:
    if not isinstance(w, dict):
        return "all data"
    parts = [",".join(w.get("dates") or [])]
    b = w.get("blocks")
    if isinstance(b, dict):
        parts.append(f"blocks {b['index'][0]}-{b['index'][-1]}/{b['n_blocks']}")
    return " ".join(x for x in parts if x) or "all data"


def _pm(x: dict[str, Any], fmt: str = "+.2f") -> str:
    return f"{x['value']:{fmt}} [{x['ci'][0]:{fmt}}, {x['ci'][1]:{fmt}}]"


def render(rep: dict[str, Any]) -> str:
    stale = rep.get("fills_after_cancel_time") or {}
    lines = [
        f"nautilus_trader {rep['nautilus_trader']}; calibration {rep['calibration_run_id']} ({rep['family']}, "
        f"p = {rep['p']:.6g}) fitted on {_window(rep['train'])}, held out on {_window(rep['test'])}; "
        f"{rep['n_shadows']} orders ({rep['n_submitted']} submitted), calibrated model over {rep['seeds']} seeds; "
        f"orders first filled after the harness's cancel time: default {stale.get(DEFAULT, '-')}, calibrated "
        f"{stale.get(CALIBRATED, '-')} (all seeds)",
        "",
        "| h | exact | NT default bias pp [95% CI] | NT calibrated bias pp [95% CI] | per-seed bias pp min / max | "
        "abs-bias reduction pp [95% CI] | one run: abs-bias reduction pp [95% CI] | Brier default / seed mean / one "
        "run | one run: Brier gain [95% CI] | NT minus zoo pp [95% CI] | default agrees with zoo p=1 |",
        "|" + "---|" * 11,
    ]
    for name, r in rep["horizons"].items():
        d, c, pr = r["default"], r["calibrated"], r["paired"]
        rp = r.get("reproduction")
        lines.append(
            f"| {name} | {100 * r['exact_rate']:.2f}% | {d['bias_pp']:+.2f} [{d['bias_ci_pp'][0]:+.2f}, "
            f"{d['bias_ci_pp'][1]:+.2f}] | {c['bias_pp']:+.2f} [{c['bias_ci_pp'][0]:+.2f}, "
            f"{c['bias_ci_pp'][1]:+.2f}] | "
            f"{c['bias_pp_per_seed']['min']:+.2f} / {c['bias_pp_per_seed']['max']:+.2f} | "
            f"{_pm(pr['abs_bias_reduction_pp'])} | {_pm(pr['abs_bias_reduction_single_run_pp'])} | "
            f"{d['brier']:.4f} / {c['brier_seed_mean']:.4f} / {c['brier_single_seed_mean']:.4f} | "
            f"{_pm(pr['brier_gain_single_run'], '+.4f')} | {_pm(rp['nt_minus_zoo_pp']) if rp else '-'} | "
            f"{100 * d['agree_with_zoo_p1']:.2f}% |"
        )
    return "\n".join(lines) + "\n"


def cmd_score(a: argparse.Namespace) -> None:
    work = Path(a.work)
    doc = _calibration_doc(a.calibration)
    zoo = (
        Path(a.zoo_held_out) if a.zoo_held_out else Path(a.calibration).parent / "held_out" / "zoo_predictions.parquet"
    )
    runs = [a.run] if a.run else sorted(p.name for p in work.glob("nt-*") if (p / "heldout_outcomes.parquet").exists())
    if not runs:
        raise SystemExit(f"no nt-<version> run directory under {work}: run the nautilus step first")
    for run in runs:
        rep = score(work, run, doc, zoo_held_out=zoo)
        info = json.loads((work / run / "heldout_runs.json").read_text())
        rep["queuetruth_nautilus"] = info["queuetruth_nautilus"]
        rep["fill_models"] = {
            c: fill_model_params(next((r["fill_model"] for r in info["runs"] if r["config"] == c), None))
            for c in (DEFAULT, CALIBRATED)
        }
        rep["generated"] = dt.date.today().isoformat()
        d = work / run
        (d / "heldout_report.json").write_text(json.dumps(rep, indent=1))
        (d / "heldout_report.md").write_text(render(rep))
        print(render(rep))
        if a.results:
            res = Path(a.results)
            res.mkdir(parents=True, exist_ok=True)
            name = "heldout-nautilus-" + run.removeprefix("nt-")
            (res / f"{name}.json").write_text(json.dumps(rep, indent=1) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fit")
    p.add_argument("--spec-from", required=True, help="an existing qt.calibration_result whose spec is reused")
    p.add_argument("--dataset", required=True, help="dataset with the train and test dates")
    p.add_argument("--shadows", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--threads", type=int, default=3)
    p.set_defaults(fn=cmd_fit)

    p = sub.add_parser("prepare")
    p.add_argument("--calibration", required=True)
    p.add_argument("--dataset", required=True, help="dataset of the held-out day(s)")
    p.add_argument("--shadows", required=True, help="qt.shadow_spec parquet; rows on the test dates are kept")
    p.add_argument("--work", required=True)
    p.add_argument("--tz", default="America/New_York", help="session time zone for the date of ts_submit")
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--harness", default=str(HARNESS))
    p.set_defaults(fn=cmd_prepare)

    p = sub.add_parser("nautilus")
    p.add_argument("--calibration", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--seeds", type=int, default=8)
    p.add_argument("--procs", type=int, default=2)
    p.add_argument("--chunk", type=int, default=60)
    p.add_argument("--window-ns", type=int, default=60_000_000_000)
    p.add_argument("--force", action="store_true", help="run a calibration the trust verdict refuses")
    p.add_argument("--tag", default="", help="output directory nt-<version>-<tag> (default nt-<version>)")
    p.add_argument("--harness", default=str(HARNESS))
    p.set_defaults(fn=cmd_nautilus)

    p = sub.add_parser("score")
    p.add_argument("--calibration", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--run", default=None, help="one nt-<version>[-<tag>] directory (default: every one under --work)")
    p.add_argument("--results", default=None, help="also copy heldout-nautilus-<version>.json into this directory")
    p.add_argument(
        "--zoo-held-out",
        default=None,
        help="the calibration's held-out zoo_predictions.parquet (default: held_out/ next to the calibration file)",
    )
    p.set_defaults(fn=cmd_score)

    a = ap.parse_args(argv)
    a.fn(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
