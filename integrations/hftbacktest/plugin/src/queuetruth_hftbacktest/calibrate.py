"""Calibrate hftbacktest's ProbQueue model against exact-queue truth and get paste-ready code: `calibrate`.

One call runs, for each power family (`power3`, `power2`, `power` by default; hftbacktest's
`power_prob_queue_model3`, `power_prob_queue_model2`, `power_prob_queue_model`), queuetruth's allocation
calibration (`queuetruth calibrate alloc`: the fitted `n`, its bootstrap and profile intervals, the held-out Brier
score, log loss and fill-rate bias of the fitted value and of the default `n = 2`, and the fail-safe trust verdict).
Without `orders`, the orders the fit uses are sampled first (`queuetruth shadows sample`, its defaults: at the touch
and behind it, both sides). The fits are compared as the calibration how-to says: among the families queuetruth's
trust check lets through, the one with the smallest held-out log loss at `horizon_ns` (60 s by default) wins, not
the one with the largest training likelihood (the families' likelihoods have different scales).

The winner's code is queuetruth's own `queuetruth export hftbacktest` snippet (`hbt_queue_model.py` in `out`): a
`BacktestAsset` chain with the fitted `.power_prob_queue_model*(n)` and the exchange model of the fit, under a
header with the fit, its intervals and the held-out verdict. It defines `asset`, so it also works as the `snippet`
of `queuetruth_hftbacktest.run`, which runs it in hftbacktest itself on orders `score` then checks.

When every family is refused, `HbtCalibration.code` raises `CalibrationRefused` (`QT-CALIB-001`); with
`force=True` the family with the smallest held-out log loss is exported anyway, under queuetruth's
`# !! NOT VALIDATED` header. Use that to inspect a fit, not to paste it.
"""

from __future__ import annotations

import json
import math
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from queuetruth_hftbacktest.errors import CalibrationRefused, QtHbtError

CALIBRATION_SCHEMA = "qthbt.calibration"
SCHEMA_VERSION = 1
FAMILIES = ("power3", "power2", "power")
BUILDERS = {
    "power": "power_prob_queue_model",
    "power2": "power_prob_queue_model2",
    "power3": "power_prob_queue_model3",
}
HORIZON_NS = 60_000_000_000
#: queuetruth's default held-out horizons (`CalibrationSpec.horizons_ns`) and longest horizon (`h_max_ns`).
DEFAULT_HORIZONS_NS = (100_000_000, 1_000_000_000, 10_000_000_000, 60_000_000_000)
H_MAX_NS = 300_000_000_000
#: `param_name`s of a power-family fit reported on the ln n scale (as `queuetruth.export`).
LOG_N_NAMES = ("ln_n", "log_n", "theta")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_BLOCKS = re.compile(r"blocks:(\d+)(?:-(\d+))?/(\d+)")


def window(w: str | list[str] | dict[str, Any] | None) -> dict[str, Any]:
    """A `WindowSpec` from the CLI forms: `2026-06-15`, `2026-06-15,2026-06-16`, `blocks:0-3/5` (blocks 0 to 3 of
    5 equal blocks of each session), a list of dates, a `WindowSpec` dict, or None (all data)."""
    if w is None:
        return {}
    if isinstance(w, dict):
        return dict(w)
    if isinstance(w, list):
        dates = [str(d) for d in w]
    else:
        m = _BLOCKS.fullmatch(w.strip())
        if m:
            lo, hi, n = int(m[1]), int(m[2] if m[2] is not None else m[1]), int(m[3])
            if not lo <= hi < n:
                raise QtHbtError("QT-CONFIG-001", f"window {w!r}: expected blocks:LO-HI/N with LO <= HI < N")
            return {"blocks": {"index": list(range(lo, hi + 1)), "n_blocks": n}}
        dates = [d.strip() for d in w.split(",") if d.strip()]
    bad = [d for d in dates if not _DATE.fullmatch(d)]
    if not dates or bad:
        raise QtHbtError("QT-CONFIG-001", f"window {w!r}: expected YYYY-MM-DD[,YYYY-MM-DD...] or blocks:LO-HI/N")
    return {"dates": dates}


@dataclass(frozen=True)
class FamilyFit:
    """One family's calibration (from its `calibration.json`)."""

    family: str
    builder: str
    n: float | None
    ci_bootstrap: tuple[float | None, float | None] | None
    n_events: int | None
    verdict: str
    reasons: list[str]
    held_out: dict[str, dict[str, float | None]]
    baseline_held_out: dict[str, dict[str, float | None]]
    path: Path

    @property
    def exportable(self) -> bool:
        return self.verdict == "exportable"

    def logloss(self, horizon_ns: int) -> float | None:
        v = self.held_out.get("logloss", {}).get(str(horizon_ns))
        return None if v is None or not math.isfinite(v) else float(v)


@dataclass
class HbtCalibration:
    """The result of `calibrate` (`hbt_calibration.json` in `out`)."""

    fits: list[FamilyFit]
    best: FamilyFit | None
    forced: bool
    horizon_ns: int
    out: Path
    shadows: Path
    _code: str | None = field(default=None, repr=False)

    @property
    def code(self) -> str:
        """Paste-ready hftbacktest code (`hbt_queue_model.py`); `CalibrationRefused` when no family passes."""
        if self._code is None:
            raise CalibrationRefused(
                "no fitted queue model passes queuetruth's trust check: nothing safe to paste",
                {f.family: f.reasons for f in self.fits},
                "keep n = 2, fit on more data or orders closer to yours; calibrate(..., force=True) writes the best "
                "fit under a NOT VALIDATED header for inspection",
            )
        return self._code

    @property
    def code_path(self) -> Path | None:
        p = self.out / "hbt_queue_model.py"
        return p if self._code is not None else None

    def to_document(self) -> dict[str, Any]:
        return {
            "schema": CALIBRATION_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "horizon_ns": self.horizon_ns,
            "shadows": str(self.shadows),
            "best": None if self.best is None else self.best.family,
            "forced": self.forced,
            "code": None if self._code is None else "hbt_queue_model.py",
            "fits": [
                {
                    "family": f.family,
                    "builder": f.builder,
                    "n": f.n,
                    "ci_bootstrap": None if f.ci_bootstrap is None else list(f.ci_bootstrap),
                    "n_events": f.n_events,
                    "verdict": f.verdict,
                    "reasons": f.reasons,
                    "held_out": f.held_out,
                    "baseline_held_out": f.baseline_held_out,
                    "calibration": _rel(f.path, self.out),
                }
                for f in self.fits
            ],
        }

    def text(self) -> str:
        """Plain-words summary: the winner first, then every family against the default `n = 2`."""
        h = str(self.horizon_ns)
        hd = _dur(self.horizon_ns)
        if self.best is None:
            head = "no fitted queue model passes queuetruth's trust check; keep n = 2 (reasons below)"
        else:
            b = self.best
            if self.forced:
                head = (
                    f"NOT VALIDATED: no family passes the trust check; .{b.builder}({_g6(b.n)}) has the smallest "
                    f"held-out log loss at {hd} (forced, for inspection only)"
                )
            else:
                head = f"use .{b.builder}({_g6(b.n)}): smallest held-out log loss at {hd} among the exportable fits"
        lines = [
            head,
            f"{'family':<7} {'n':<9} {'95% CI (bootstrap)':<24} {f'log loss {hd}, fitted / n=2':<28} "
            f"{f'fill-rate bias {hd}, fitted / n=2':<33} verdict",
        ]
        for f in self.fits:
            ci = "" if f.ci_bootstrap is None else f"[{_g6(f.ci_bootstrap[0])}, {_g6(f.ci_bootstrap[1])}]"
            ll = f"{_g5(f.held_out.get('logloss', {}).get(h))} / {_g5(f.baseline_held_out.get('logloss', {}).get(h))}"
            fb_fit = _pp(f.held_out.get("fill_rate_bias", {}).get(h))
            fb = f"{fb_fit} / {_pp(f.baseline_held_out.get('fill_rate_bias', {}).get(h))}"
            v = f.verdict + ("" if not f.reasons else f" ({', '.join(f.reasons)})")
            lines.append(f"{f.family:<7} {_g6(f.n):<9} {ci:<24} {ll:<28} {fb:<33} {v}")
        if self._code is not None:
            lines.append(f"code: {self.out / 'hbt_queue_model.py'}")
        return "\n".join(lines) + "\n"


def _rel(p: Path, base: Path) -> str:
    return str(p.relative_to(base)) if p.is_relative_to(base) else str(p)


def _g6(x: float | None) -> str:
    return "n/a" if x is None else format(float(x), ".6g")


def _g5(x: float | None) -> str:
    return "n/a" if x is None else f"{float(x):.5f}"


def _pp(x: float | None) -> str:
    return "n/a" if x is None else f"{float(x) * 100:+.2f} pp"


def _dur(ns: int) -> str:
    s = ns / 1e9
    return f"{int(s // 60)} min" if s >= 60 and s % 60 == 0 else f"{s:g} s"


def _fit(family: str, path: Path) -> FamilyFit:
    try:
        c = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise QtHbtError("QT-INPUT-001", f"{path}: {e}") from e
    trust = c.get("trust") or {}
    held = next(iter((c.get("held_out") or {}).values()), {}) or {}
    base = next(iter((c.get("baseline_held_out") or {}).values()), {}) or {}
    ci = c.get("ci_bootstrap")
    est = c.get("estimate")
    if est is not None and math.isfinite(float(est)) and str(c.get("param_name") or "").lower() in LOG_N_NAMES:
        est = math.exp(float(est))  # a fit on the ln n scale (queuetruth.export: `power_n`)
    return FamilyFit(
        family=family,
        builder=BUILDERS[family],
        n=None if est is None or not math.isfinite(float(est)) else float(est),
        ci_bootstrap=None if not isinstance(ci, list) or len(ci) != 2 else (ci[0], ci[1]),
        n_events=c.get("n_events"),
        verdict=str(trust.get("verdict") or "refused"),
        reasons=[str(r) for r in trust.get("reasons") or []],
        held_out={k: dict(v) for k, v in held.items() if isinstance(v, dict)},
        baseline_held_out={k: dict(v) for k, v in base.items() if isinstance(v, dict)},
        path=path,
    )


def select(fits: list[FamilyFit], horizon_ns: int, *, force: bool = False) -> tuple[FamilyFit | None, bool]:
    """(winner, forced): the exportable fit with the smallest held-out log loss at `horizon_ns` (ties and missing
    log losses go to the earlier family in `fits`); with none exportable and `force`, the same rule over every fit
    with an estimate, and `forced` is True."""

    def rank(i_f: tuple[int, FamilyFit]) -> tuple[float, int]:
        ll = i_f[1].logloss(horizon_ns)
        return (math.inf if ll is None else ll, i_f[0])

    if fits and all(f.logloss(horizon_ns) is None for f in fits):
        raise QtHbtError(
            "QT-CONFIG-001",
            f"no fit has a held-out log loss at {_dur(horizon_ns)}, so none can be picked on it",
            "the test window scores only orders whose planned life covers the horizon: use a shorter horizon or "
            "orders that live longer",
        )
    ok = [(i, f) for i, f in enumerate(fits) if f.exportable and f.n is not None]
    if ok:
        return min(ok, key=rank)[1], False
    if force:
        cands = [(i, f) for i, f in enumerate(fits) if f.n is not None]
        if cands:
            return min(cands, key=rank)[1], True
    return None, False


def calibrate(
    data: str | Path,
    train: str | list[str] | dict[str, Any],
    test: str | list[str] | dict[str, Any] | None = None,
    *,
    out: str | Path,
    orders: str | Path | None = None,
    families: tuple[str, ...] | list[str] = FAMILIES,
    exchange: str = "partial",
    horizon_ns: int = HORIZON_NS,
    n_orders: int = 2000,
    seed: int = 0,
    threads: int = 1,
    bootstrap_b: int | None = None,
    force: bool = False,
) -> HbtCalibration:
    """Fit hftbacktest's ProbQueue `n` per family on `train`, score it on `test`, pick the winner and write its code
    (module doc). `orders` (a `qt.shadow_spec` file) are the orders the fit uses; without them `n_orders` are sampled
    with `seed`. `exchange` is `partial` (`partial_fill_exchange`) or `no_partial`. Writes `cal-<family>/`, the
    sampled `shadows.parquet`, `hbt_calibration.json` and `hbt_queue_model.py` to `out`."""
    import queuetruth as qt

    data, out = Path(data), Path(out)
    if not (data / "manifest.json").exists():
        raise QtHbtError("QT-INPUT-001", f"data: {data} is not a queuetruth dataset directory (no manifest.json)")
    fams = list(dict.fromkeys(families))
    bad = [f for f in fams if f not in BUILDERS]
    if not fams or bad:
        raise QtHbtError("QT-CONFIG-001", f"families: expected some of {list(BUILDERS)}, got {list(families)}")
    if exchange not in ("partial", "no_partial"):
        raise QtHbtError("QT-CONFIG-001", f"exchange: expected partial or no_partial, got {exchange!r}")
    if horizon_ns < 1:
        raise QtHbtError("QT-CONFIG-001", f"horizon_ns: expected >= 1, got {horizon_ns}")
    tr, te = window(train), window(test)
    out.mkdir(parents=True, exist_ok=True)
    # a code file from an earlier run must not outlive a refusal in this one
    (out / "hbt_queue_model.py").unlink(missing_ok=True)
    if orders is None:
        shadows = out / "shadows.parquet"
        qt.sample_shadows({"dataset": {"path": str(data)}, "n": int(n_orders), "seed": int(seed), "out": str(shadows)})
    else:
        shadows = Path(orders)
        if not shadows.exists():
            raise QtHbtError("QT-INPUT-001", f"orders: {shadows} does not exist")
    fits: list[FamilyFit] = []
    for fam in fams:
        cspec: dict[str, Any] = {
            "kind": "alloc",
            "family": fam,
            "dataset": {"path": str(data)},
            "shadows": {"path": str(shadows)},
            "train": tr,
            "test": te,
            "exchange": exchange,
            "horizons_ns": sorted({*DEFAULT_HORIZONS_NS, int(horizon_ns)}),
            "h_max_ns": max(H_MAX_NS, int(horizon_ns)),
            "seed": int(seed),
            "threads": int(threads),
            "out": str(out / f"cal-{fam}"),
        }
        if bootstrap_b is not None:
            cspec["bootstrap"] = {"b": int(bootstrap_b)}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # the verdict and its reasons are read from calibration.json
            qt.calibrate(cspec)
        fits.append(_fit(fam, out / f"cal-{fam}" / "calibration.json"))

    best, forced = select(fits, horizon_ns, force=force)
    code = None
    if best is not None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            spec = {
                "target": "hftbacktest",
                "calibration": {"path": str(best.path)},
                "out": str(out / "hbt_queue_model.py"),
            }
            summ = qt.export(spec, force=forced)
        code = summ.text
    res = HbtCalibration(
        fits=fits, best=best, forced=forced, horizon_ns=int(horizon_ns), out=out, shadows=shadows, _code=code
    )
    (out / "hbt_calibration.json").write_text(
        json.dumps(res.to_document(), indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    return res
