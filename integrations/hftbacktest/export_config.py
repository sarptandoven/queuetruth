#!/usr/bin/env python3
"""Export queuetruth calibrations to an hftbacktest queue-model config (`qtint.hbt_queue_config` v1).

Runs in the queuetruth environment. Each `--calibration` is a `qt.calibration_result` (`calibration.json`, or a
directory holding one) of a power family (`power`, `power2`, `power3`; allocation quasi-likelihood, spec 5.4 C1).
For each one the config gets:

  role "fitted"   the fitted exponent n (full precision), its CIs (on the n scale), events, the per-stratum estimates
                  (`stratified`), the held-out metrics, the trust verdict and every trust flag; the non-refusing
                  flags `plateau`, `profile_ci_open` and `heterogeneous_strata` and the held-out summary also become
                  notes, with the texts of `queuetruth export hftbacktest`'s header;
  role "default"  the same family at the zoo default n = 2 (with `--defaults`), the baseline the held-out
                  comparison used, with its held-out metrics;
  role "fixed"    with `--log`: `log_prob_queue_model()` and `log_prob_queue_model2()`. The log families have no
                  parameter; the calibration reports their quasi log likelihood on the same train events as the
                  fitted family (`info: comparison quasi log likelihood ...` lines), so they can be ranked with it
                  (`train_quasi_loglik`, higher is better). That likelihood scores where cancellations come from; it
                  is not the fill-rate target (compare the held-out metrics and `reconcile.py`'s realism tables).

Several `--calibration`s must share one comparison basis (dataset ids, shadows, train window, L2 view, exchange,
mode and train events), otherwise their likelihoods are not comparable and the export refuses (`QT-CONFIG-001`).
A calibration that `queuetruth.export.trust.assess` refuses is not exported (`QT-CALIB-001`, exit 1, as `queuetruth
export`) unless `--force`; a forced entry carries a `NOT VALIDATED` note with the refusal reasons. Other errors exit 2.

Usage:
  python integrations/hftbacktest/export_config.py --calibration out/calib-power3 --log --defaults \
      --out hbt_queue_config.json
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hbt_config

from queuetruth import __version__
from queuetruth.errors import EXIT_ERROR, QtError
from queuetruth.export import load_calibration, trust
from queuetruth.export.hftbacktest import NOTE_316, l2_note
from queuetruth.schemas.models import CalibrationResult

POWER_FAMILIES = ("power", "power2", "power3")
DEFAULT_N = 2.0
LOGLIK_LINE = re.compile(r"^info: comparison quasi log likelihood hbt_(log2?) = (\S+) \((\d+) events\)$")
#: `param_name` values meaning the estimate is on the ln n scale (spec 5.4 C1 optimizes theta = ln n).
LOG_N_NAMES = ("ln_n", "log_n", "theta")
#: Non-refusing trust flags that `queuetruth export hftbacktest` states in its header (`trust.header_lines`).
NOTE_FLAGS = ("plateau", "profile_ci_open", "heterogeneous_strata")

# The three helpers below restate queuetruth.export._text (a private module) so that this script depends only on
# public queuetruth APIs; tests/test_export_config.py checks that they agree with it.


def family_key(family: str) -> str:
    """Normalized family: `hbt_power3` -> `power3`, `databento_mc` -> `databento`."""
    f = family.strip().lower()
    if f.startswith("hbt_"):
        f = f[4:]
    return "databento" if f == "databento_mc" else f


def on_n_scale(cal: CalibrationResult) -> bool:
    """False when `param_name` says the estimate (and its CIs) are on the ln n scale."""
    return (cal.param_name or "").lower() not in LOG_N_NAMES


def to_n(cal: CalibrationResult, x: float | None) -> float | None:
    """A value of the calibration's parameter on the n scale (`exp` for the ln n names); None stays None."""
    if x is None or not math.isfinite(x):
        return None
    return float(x) if on_n_scale(cal) else math.exp(x)


def power_n(cal: CalibrationResult) -> float | None:
    """`n` of a power-family fit."""
    return to_n(cal, cal.estimate)


def spec_value(cal: CalibrationResult, key: str, default: str) -> str:
    """A string field of the echoed spec (e.g. `exchange`), or `default`."""
    v = cal.spec.get(key) if isinstance(cal.spec, dict) else None
    return v if isinstance(v, str) else default


NOTE_312 = (
    "hftbacktest #312: with partial_fill_exchange, a maker fill that leaves less than half a lot completes the order "
    "but leaves it in the book; its next fill attempt aborts the whole backtest with InvalidOrderStatus (elapse "
    "returns 14). See integrations/hftbacktest/UPSTREAM.md for the proposed fix."
)


def comparison_logliks(cal: CalibrationResult) -> dict[str, tuple[float, int]]:
    """`{"log": (l, events), "log2": (l, events)}` from the calibration's info lines (absent ones omitted)."""
    out: dict[str, tuple[float, int]] = {}
    for w in cal.warnings:
        m = LOGLIK_LINE.match(w)
        if m:
            v = float(m.group(2))
            if math.isfinite(v):
                out[m.group(1)] = (v, int(m.group(3)))
    return out


def _window(w: Any) -> Any:
    return w.model_dump(mode="json") if hasattr(w, "model_dump") else w


def _ci_n(cal: CalibrationResult, ci: Any) -> list[float | None] | None:
    return None if ci is None else [to_n(cal, x) for x in ci]


def _held_out(ho: Any) -> dict[str, dict[str, float | None]]:
    """`{metric: {horizon_ns: value}}` of one model's held-out metrics."""
    return {k: dict(getattr(ho, k)) for k in ("brier", "logloss", "fill_rate_bias")}


def held_out_of(cal: CalibrationResult, baseline: bool) -> dict[str, Any] | None:
    """The held-out metrics of the fitted model (or of the default, `baseline=True`) with the model id they score."""
    d = cal.baseline_held_out if baseline else cal.held_out
    for mid, ho in d.items():
        return {"model_id": mid, "test": _window(cal.test), **_held_out(ho)}
    return None


def provenance(cal: CalibrationResult, t: trust.Trust) -> dict[str, Any]:
    """The calibration facts a reader of the config needs to judge the parameter (n scale throughout)."""
    return {
        "calibration_run_id": cal.run_id,
        "calibration_family": cal.family,
        "calibration_mode": cal.mode,
        "param_name": cal.param_name,
        "estimate": cal.estimate,
        "n": power_n(cal),
        "ci_bootstrap_n": _ci_n(cal, cal.ci_bootstrap),
        "ci_profile_n": _ci_n(cal, cal.ci_profile),
        "n_events": cal.n_events,
        "n_shadows": cal.n_shadows,
        "train": _window(cal.train),
        "test": _window(cal.test),
        "l2": cal.spec.get("l2") if isinstance(cal.spec, dict) else None,
        "stratified": [
            {
                "stratum_kind": s.stratum_kind,
                "stratum": s.stratum,
                "n": to_n(cal, s.estimate),
                "ci_n": [to_n(cal, s.ci_lo), to_n(cal, s.ci_hi)],
                "events": s.n,
            }
            for s in cal.stratified
        ],
        "held_out": held_out_of(cal, baseline=False),
        "held_out_summary": trust.held_out_summary(t),
        "trust_verdict": "exportable" if t.ok else "refused",
        "trust_reasons": [k for k, _ in t.reasons],
        "trust_flags": dict(sorted(t.flags.items())),
        "data_sources": [d.model_dump(mode="json") for d in cal.data_sources],
    }


def comparison_basis(cal: CalibrationResult) -> dict[str, Any]:
    """What must be equal for two calibrations' train likelihoods to be on one scale."""
    sh = cal.spec.get("shadows") if isinstance(cal.spec, dict) else None
    return {
        "dataset_ids": sorted(d.dataset_id for d in cal.data_sources),
        "shadows": sh.get("path") if isinstance(sh, dict) else sh,
        "train": _window(cal.train),
        "l2": cal.spec.get("l2") if isinstance(cal.spec, dict) else None,
        "exchange": spec_value(cal, "exchange", "partial"),
        "mode": cal.mode,
        "n_events": cal.n_events,
    }


def entries_for(cal: CalibrationResult, *, force: bool, log: bool, defaults: bool) -> list[dict[str, Any]]:
    """Config entries for one calibration (raises `QtError` for a non-power family or a refused fit)."""
    fam = family_key(cal.family)
    if cal.kind not in ("alloc", "live") or fam not in POWER_FAMILIES:
        raise QtError(
            "QT-CONFIG-001",
            f"calibration {cal.run_id} is {cal.kind} {cal.family}: only power-family fits (power, power2, power3) "
            "map to an hftbacktest queue-model parameter",
        )
    n = power_n(cal)
    if n is None or n <= 0.0:
        raise trust.ExportRefused(f"calibration {cal.run_id} has no finite positive estimate of n")
    t = trust.assess(cal)
    notes = [f"held-out: {trust.held_out_summary(t)}"]
    notes += [f"note ({tok}): {t.flags[tok]}" for tok in NOTE_FLAGS if tok in t.flags]
    notes += [NOTE_312, NOTE_316, l2_note(cal)]
    if not t.ok:
        reasons = "; ".join(f"{k}: {v}" for k, v in t.reasons)
        if not force:
            raise trust.ExportRefused(
                f"calibration {cal.run_id} is refused by the trust check ({reasons}); pass --force to export it anyway",
            )
        notes.insert(0, f"NOT VALIDATED (exported with --force): {reasons}")
    exchange = spec_value(cal, "exchange", "partial")
    src = provenance(cal, t)
    out = [
        hbt_config.make_entry(
            fam,
            exchange,
            [n],
            "fitted",
            train_quasi_loglik=cal.loglik,
            train_events=cal.n_events,
            source=src,
            notes=notes,
        )
    ]
    if defaults and n != DEFAULT_N:
        out.append(
            hbt_config.make_entry(
                fam,
                exchange,
                [DEFAULT_N],
                "default",
                source={
                    "calibration_run_id": cal.run_id,
                    "baseline_of": out[0]["model_id"],
                    "held_out": held_out_of(cal, baseline=True),
                },
                notes=["queuetruth zoo default n = 2: the baseline of the calibration's held-out comparison."],
            )
        )
    if log:
        ll = comparison_logliks(cal)
        for lf in ("log", "log2"):
            v = ll.get(lf)
            out.append(
                hbt_config.make_entry(
                    lf,
                    exchange,
                    [],
                    "fixed",
                    train_quasi_loglik=None if v is None else v[0],
                    train_events=None if v is None else v[1],
                    source={"calibration_run_id": cal.run_id, "compared_with": out[0]["model_id"]},
                    notes=[
                        "No parameter. train_quasi_loglik is the C1 allocation quasi log likelihood on the same "
                        "train events as the fitted entry (higher is better)."
                        if v is not None
                        else "The calibration reports no comparison log likelihood for this family."
                    ],
                )
            )
    return out


def build(paths: list[str], *, force: bool, log: bool, defaults: bool) -> dict[str, Any]:
    """The config document for the calibrations at `paths` (entries in input order, duplicates dropped).

    All calibrations must share one `comparison_basis` (`QT-CONFIG-001` otherwise)."""
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    cals = [load_calibration(p) for p in paths]
    basis = comparison_basis(cals[0])
    for p, cal in zip(paths[1:], cals[1:], strict=True):
        b = comparison_basis(cal)
        if b != basis:
            diff = sorted(k for k in basis if basis[k] != b[k])
            raise QtError(
                "QT-CONFIG-001",
                f"{p} was fitted on a different basis than {paths[0]} ({', '.join(diff)} differ): their train "
                "likelihoods are not comparable; export them to separate configs",
            )
    for cal in cals:
        for e in entries_for(cal, force=force, log=log, defaults=defaults):
            if e["model_id"] not in seen:
                seen.add(e["model_id"])
                models.append(e)
    return hbt_config.document(models, __version__)


def summary_lines(doc: dict[str, Any]) -> list[str]:
    lines = []
    for m in doc["models"]:
        ll = m["train_quasi_loglik"]
        lls = "" if ll is None else f"  train quasi-loglik {ll:.6g} ({m['train_events']} events)"
        lines.append(f"{m['role']:<8} {m['model_id']}{lls}")
        lines.append(f"         asset{hbt_config.builder_chain(m)}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--calibration", action="append", required=True, help="qt.calibration_result file or dir")
    ap.add_argument("--out", type=Path, required=True, help="output qtint.hbt_queue_config JSON file")
    ap.add_argument("--log", action="store_true", help="also export log_prob_queue_model and _model2")
    ap.add_argument("--defaults", action="store_true", help="also export each family at the default n = 2")
    ap.add_argument("--force", action="store_true", help="export calibrations the trust check refuses")
    args = ap.parse_args(argv)
    try:
        doc = build(args.calibration, force=args.force, log=args.log, defaults=args.defaults)
    except QtError as e:
        print(str(e), file=sys.stderr)
        return e.exit_code  # 1 for a trust refusal (ExportRefused), 2 otherwise
    except hbt_config.ConfigError as e:
        print(str(e), file=sys.stderr)
        return EXIT_ERROR
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(hbt_config.dumps(doc))
    print("\n".join(summary_lines(doc)))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
