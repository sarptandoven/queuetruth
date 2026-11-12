"""`configure(asset, calibration)`: the queue model and exchange of a calibration, set on an hftbacktest
`BacktestAsset` in one line (standard library only: it runs in the hftbacktest venv too).

    import queuetruth as qt
    import queuetruth_hftbacktest as qthbt

    asset = qthbt.configure(asset, qt.fit("hftbacktest", data="dataset"))    # a fit on your data
    asset = qthbt.configure(asset, qt.atlas.calibration("hftbacktest", tier="T1", yes=True))  # no data: the atlas
    asset = qthbt.configure(asset, "qt-fit/fit.json")                        # in another venv: the saved document

`calibration` is any of:

- the `FitResult` of `qt.fit("hftbacktest", ...)` or `qt.atlas.calibration("hftbacktest", ...)`, its `qt.fit`
  document (`.to_dict()`), or the path of a `fit.json` (or the directory holding it);
- the `HbtCalibration` of this package's `calibrate` (its best family).

It calls the builder the calibration names (`power_prob_queue_model3(n)`, `log_prob_queue_model()`, ...) and the
exchange (`no_partial_fill_exchange`, the one that runs on the released hftbacktest 2.4.4; `partial_fill_exchange`
stops it with error 14, hftbacktest #312) and returns the asset. Keep your data, tick, lot, latency and fee
settings: they are not touched.

A fit whose verdict is refused raises `CalibrationRefused` (`QT-CALIB-001`); `force=True` applies it with a
`CalibrationWarning` instead. An atlas setting always warns that it is the atlas's closest setting for its tier, not
a fit on your data, and whether it is still clearly off.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

from queuetruth_hftbacktest.errors import CalibrationRefused, CalibrationWarning, QtHbtError

FIT_SCHEMA = "qt.fit"
#: The power families' builders (queuetruth.export.hftbacktest) and the parameter-free ones.
POWER_BUILDERS = {
    "power": "power_prob_queue_model",
    "power2": "power_prob_queue_model2",
    "power3": "power_prob_queue_model3",
}
FIXED_BUILDERS = {
    "log": "log_prob_queue_model",
    "log2": "log_prob_queue_model2",
    "risk_adverse": "risk_adverse_queue_model",
}
EXCHANGES = ("no_partial_fill_exchange", "partial_fill_exchange")
DEFAULT_EXCHANGE = "no_partial_fill_exchange"


def _read_fit(path: Path) -> dict[str, Any]:
    p = path / "fit.json" if path.is_dir() else path
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise QtHbtError("QT-INPUT-001", f"{p}: no such file") from e
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise QtHbtError("QT-INPUT-002", f"{p}: not a readable JSON document ({e})") from e
    if not isinstance(doc, dict) or doc.get("schema") != FIT_SCHEMA:
        raise QtHbtError("QT-INPUT-002", f"{p}: not a qt.fit document (fit.json of qt.fit or qt.atlas.calibration)")
    return doc


def _fit_doc(calibration: Any) -> dict[str, Any] | None:
    """The `qt.fit` document behind `calibration`, or None when it is not one."""
    if isinstance(calibration, str | Path):
        return _read_fit(Path(calibration))
    doc = getattr(calibration, "doc", None)
    if isinstance(doc, dict) and doc.get("schema") == FIT_SCHEMA:
        return doc
    if isinstance(calibration, dict) and calibration.get("schema") == FIT_SCHEMA:
        return calibration
    return None


def _settings_of(doc: dict[str, Any]) -> dict[str, Any]:
    """`doc["settings"]`, or the same from the family and value of a document written before it had them."""
    st = doc.get("settings")
    if isinstance(st, dict):
        return st
    fam, value = str(doc.get("family") or ""), (doc.get("param") or {}).get("value")
    if fam in POWER_BUILDERS and isinstance(value, int | float):
        return {"queue_model": POWER_BUILDERS[fam], "queue_model_args": [float(value)], "exchange": DEFAULT_EXCHANGE}
    if fam in FIXED_BUILDERS:
        return {"queue_model": FIXED_BUILDERS[fam], "queue_model_args": [], "exchange": DEFAULT_EXCHANGE}
    return {}


def _from_fit(doc: dict[str, Any], force: bool) -> tuple[str, list[float], str, list[str]]:
    """(builder, args, exchange, warnings) of a `qt.fit` document."""
    if doc.get("backtester") != "hftbacktest":
        raise QtHbtError(
            "QT-CONFIG-001",
            f"this is a {doc.get('backtester')} setting, not an hftbacktest one",
            'fit or look up hftbacktest: qt.fit("hftbacktest", ...) or qt.atlas.calibration("hftbacktest", ...)',
        )
    notes: list[str] = []
    st = _settings_of(doc)
    v = doc.get("verdict") or {}
    reasons = [str(r.get("code")) for r in v.get("reasons") or [] if isinstance(r, dict)]
    if not st and "no_library_setting" not in reasons:
        reasons.append("no_library_setting")
    a = doc.get("atlas") if doc.get("source") == "atlas" else None
    if not v.get("passed") or not st:
        what = "the atlas setting" if a is not None else f"the {doc.get('family')} fit"
        if not force or not st:
            raise CalibrationRefused(
                f"{what} is refused: {', '.join(reasons) or 'refused'} ({doc.get('answer', '')})",
                {str(doc.get("family")): reasons},
                "keep your queue model and fit on more data or on orders closer to yours"
                + ("; force=True applies it anyway, for inspection only" if st else ""),
            )
        notes.append(f"force: using a NOT VALIDATED setting ({', '.join(reasons)})")
    if a is not None:
        notes.append(
            f"from the Fill-Realism Atlas ({a.get('venue_name')}, {a.get('window')}, tier {a.get('tier')}, within "
            f"{a.get('horizon')}), not your data: {a.get('verdict_text')}; fit on your own data with "
            'qt.fit("hftbacktest", data=...) to confirm'
        )
    args = [float(x) for x in st.get("queue_model_args") or []]
    return str(st["queue_model"]), args, str(st.get("exchange") or DEFAULT_EXCHANGE), notes


def _from_hbt(cal: Any, force: bool) -> tuple[str, list[float], str, list[str]]:
    """(builder, args, exchange, warnings) of this package's `HbtCalibration`."""
    best = cal.best
    if best is None:
        raise CalibrationRefused(
            "no fitted queue model passes queuetruth's trust check: nothing to configure",
            {f.family: f.reasons for f in cal.fits},
            "keep n = 2, fit on more data or orders closer to yours",
        )
    notes = []
    if cal.forced:
        if not force:
            raise CalibrationRefused(
                f"the {best.family} fit is NOT VALIDATED (calibrate(..., force=True) picked it for inspection)",
                {best.family: best.reasons},
                "configure(..., force=True) applies it anyway, for inspection only",
            )
        notes.append(f"force: using a NOT VALIDATED fit ({', '.join(best.reasons)})")
    return best.builder, [float(best.n)], DEFAULT_EXCHANGE, notes


def settings(calibration: Any, *, force: bool = False) -> dict[str, Any]:
    """What `configure` sets: `queue_model`, `queue_model_args`, `exchange` and the `warnings` it emits."""
    doc = _fit_doc(calibration)
    if doc is not None:
        builder, args, exchange, notes = _from_fit(doc, force)
    elif hasattr(calibration, "best") and hasattr(calibration, "fits"):
        builder, args, exchange, notes = _from_hbt(calibration, force)
    else:
        raise QtHbtError(
            "QT-CONFIG-001",
            f"expected a qt.fit or qt.atlas.calibration result, a fit.json path or an HbtCalibration, got "
            f"{type(calibration).__name__}",
        )
    if builder not in {*POWER_BUILDERS.values(), *FIXED_BUILDERS.values()} or exchange not in EXCHANGES:
        raise QtHbtError("QT-INPUT-002", f"unknown hftbacktest setting .{builder}(...).{exchange}()")
    return {"queue_model": builder, "queue_model_args": args, "exchange": exchange, "warnings": notes}


def configure(asset: Any, calibration: Any, *, force: bool = False) -> Any:
    """Sets the calibration's queue model and exchange on `asset` (an hftbacktest `BacktestAsset`) and returns it."""
    s = settings(calibration, force=force)
    missing = [m for m in (s["queue_model"], s["exchange"]) if not callable(getattr(asset, m, None))]
    if missing:
        raise QtHbtError(
            "QT-CONFIG-001",
            f"{type(asset).__name__} has no {', '.join(missing)}: expected an hftbacktest BacktestAsset",
        )
    out = getattr(asset, s["queue_model"])(*s["queue_model_args"])
    out = getattr(asset if out is None else out, s["exchange"])()
    for n in s["warnings"]:
        warnings.warn(CalibrationWarning(n), stacklevel=2)
    return asset if out is None else out


__all__ = ["configure", "settings"]
