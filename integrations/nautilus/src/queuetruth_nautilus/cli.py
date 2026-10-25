"""`queuetruth-nautilus`: inspect a calibration and print version-correct NautilusTrader code.

  queuetruth-nautilus inspect CAL [--format text|json] [--force]
      what the calibration sets on a NautilusTrader venue, its trust verdict and held-out scores; with
      nautilus_trader installed it also builds the objects (proving the installed version accepts them).
      `--format json` prints one `qt.nautilus_inspect` document (schema: `schemas/qt.nautilus_inspect.schema.json`)
  queuetruth-nautilus snippet CAL [--nt-major 1|2] [--seed N] [--force]
      Python lines for the release line (default: the installed nautilus_trader)
  queuetruth-nautilus env [--format text|json]
      the installed nautilus_trader version, whether it is a cross-checked line, and the class names used

`--json` is an alias of `--format json`. Exit codes as `queuetruth`: 0 success, 1 refused calibration
(`QT-CALIB-001`), 2 any other error (an unexpected exception is `QT-ENGINE-001`). As in `queuetruth`, an error prints
`<code>: <message>` and `hint: ...` on stderr, or with `--format json` exactly one `qt.error` document on stdout
(`inspect` of a refused calibration prints its `qt.nautilus_inspect` document, whose `refused` field is the error).
`CalibrationWarning`s go to stderr as `warning: ...`.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from typing import Any

from queuetruth_nautilus import __version__, compat
from queuetruth_nautilus.calibration import NautilusCalibration, load_calibration
from queuetruth_nautilus.errors import EXIT_ERROR, EXIT_OK, CalibrationRefused, CalibrationWarning, QtNautilusError
from queuetruth_nautilus.snippet import render
from queuetruth_nautilus.venue import check_trust, venue_options

#: `schema` of the `inspect --format json` document (JSON Schema in `schemas/`)
INSPECT_SCHEMA = "qt.nautilus_inspect"


def _touch_note(cal: NautilusCalibration) -> str | None:
    """A pointer to the configuration that needs no fit, for a touch-model calibration."""
    if cal.prob_fill_on_limit is None or cal.queue_position:
        return None
    return (
        "this is NautilusTrader's touch model (queue_position=False). Where the feed carries L2 depth and trade "
        "prints, queue_position=True at p = 1 needs no fit and was closer to exact per order in queuetruth's "
        "cross-check; score both on your data"
    )


def _dur(ns: int) -> str:
    for unit, size in (("s", 1_000_000_000), ("ms", 1_000_000)):
        if ns >= size and ns % size == 0:
            return f"{ns // size}{unit}"
    return f"{ns}ns"


def _text(cal: NautilusCalibration, built: dict[str, Any] | None, nt: str | None, refused: bool) -> str:
    s = cal.summary()
    lines = [
        f"calibration {cal.run_id}: {cal.family} ({cal.kind}, {cal.mode}), from {cal.source_format}",
        f"fitted on {cal.train}; held-out {cal.test}",
        f"prob_fill_on_limit: {'not fitted' if cal.prob_fill_on_limit is None else repr(cal.prob_fill_on_limit)}",
        f"queue_position: {cal.queue_position}",
    ]
    lat = s["latency"]
    if lat is None or all(
        lat[k] is None for k in ("insert_latency_nanos", "update_latency_nanos", "cancel_latency_nanos")
    ):
        lines.append("latency: not fitted")
    else:
        lines.append(
            "latency (p50 one-way ns): "
            + ", ".join(
                f"{k}={'-' if lat[k] is None else lat[k]}"
                for k in ("insert_latency_nanos", "update_latency_nanos", "cancel_latency_nanos")
            )
        )
    if cal.n_events is not None:
        lines.append(f"events: {cal.n_events} from {cal.n_shadows} shadows; bootstrap CI {s['ci_bootstrap']}")
    t = cal.trust
    lines.append(f"trust: {t.verdict}" + (f" ({', '.join(t.reasons)})" if t.reasons else "") + f" [{t.source}]")
    if cal.held_out is not None and cal.baseline_held_out is not None:
        lines.append(
            f"held-out fill-rate bias, fitted {cal.held_out.model_id} vs default {cal.baseline_held_out.model_id}:"
        )
        for h in sorted(cal.held_out.fill_rate_bias):
            f = cal.held_out.fill_rate_bias[h]
            d = cal.baseline_held_out.fill_rate_bias.get(h)
            fb = cal.held_out.brier.get(h)
            db = cal.baseline_held_out.brier.get(h)
            dtxt = "-" if d is None else f"{100 * d:+.2f} pp"
            btxt = "" if fb is None or db is None else f"; Brier {fb:.4g} vs {db:.4g}"
            lines.append(f"  {_dur(h)}: {100 * f:+.2f} pp vs {dtxt}{btxt}")
    note = _touch_note(cal)
    if note is not None:
        lines.append(f"note: {note}")
    if refused:
        lines.append("objects not built: the trust verdict refuses this calibration (--force builds them)")
    elif built is not None:
        lines.append(f"nautilus_trader {nt}: built " + ", ".join(f"{k}={_obj(v)}" for k, v in built.items()))
    elif nt is None:
        lines.append("nautilus_trader: not installed (objects not built)")
    return "\n".join(lines) + "\n"


def _obj(v: Any) -> str:
    if isinstance(v, bool):
        return repr(v)
    name = getattr(v, "name", None)
    if isinstance(name, str) and type(v).__name__ == "BookType":
        return f"BookType.{name}"
    return compat.describe(v)


def _nt_version() -> str | None:
    """Installed nautilus_trader version text, None when it is not installed (other version errors propagate)."""
    try:
        return compat.installed_version().text
    except QtNautilusError as e:
        if "not installed" in e.message:
            return None
        raise


def cmd_inspect(a: argparse.Namespace) -> int:
    cal = load_calibration(a.calibration)
    nt = _nt_version()
    refused: CalibrationRefused | None = None
    built: dict[str, Any] | None = None
    try:
        if nt is None:
            check_trust(cal, force=a.force)
        else:
            # random_seed=0: the objects are built to prove the installed version takes them, not to run
            built = venue_options(cal, force=a.force, random_seed=0)
    except CalibrationRefused as e:
        refused = e
    if a.format == "json":
        doc = {"schema": INSPECT_SCHEMA, "schema_version": 1, "calibration": cal.summary()}
        doc["nautilus_trader"] = nt
        doc["venue_options"] = None if built is None else {k: _obj(v) for k, v in built.items()}
        doc["refused"] = None if refused is None else refused.to_document()
        doc["note"] = _touch_note(cal)
        print(json.dumps(doc, indent=1, sort_keys=False))
        if refused is not None:
            return refused.exit_code  # the document carries the error: one document on stdout
    else:
        sys.stdout.write(_text(cal, built, nt, refused is not None))
    if refused is not None:
        raise refused
    return EXIT_OK


def cmd_snippet(a: argparse.Namespace) -> int:
    cal = load_calibration(a.calibration)
    if a.nt_major is not None:
        major = a.nt_major
    else:
        try:
            major = compat.installed_version().major
        except QtNautilusError as e:
            raise QtNautilusError(e.code, e.message, f"{e.hint}; or pass --nt-major 1|2", e.details) from e
    sys.stdout.write(render(cal, major, random_seed=a.seed, force=a.force))
    return EXIT_OK


def cmd_env(a: argparse.Namespace) -> int:
    v = compat.installed_version()
    doc = {
        "queuetruth_nautilus": __version__,
        "nautilus_trader": v.text,
        "verified_line": v.verified,
        "fill_model": ":".join(compat.fill_model_path(v)),
        "latency_model": ":".join(compat.latency_model_path(v)),
    }
    if a.format == "json":
        print(json.dumps(doc, indent=1))
    else:
        for k, val in doc.items():
            print(f"{k}: {val}")
    return EXIT_OK


def _format_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--format", choices=("text", "json"), default="text", help="output format (default: text)")
    p.add_argument("--json", dest="format", action="store_const", const="json", help="alias of --format json")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="queuetruth-nautilus", description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version=f"queuetruth-nautilus {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("inspect", help="what a calibration sets on a NautilusTrader venue")
    p.add_argument("calibration", help="calibration.json, a directory holding one, or an export snippet")
    _format_args(p)
    p.add_argument("--force", action="store_true", help="use a calibration the trust verdict refuses")
    p.set_defaults(fn=cmd_inspect)
    p = sub.add_parser("snippet", help="version-correct NautilusTrader code")
    p.add_argument("calibration")
    p.add_argument("--nt-major", type=int, choices=(1, 2), default=None)
    p.add_argument("--seed", type=int, default=None, help="random_seed of the fill model (not fitted)")
    p.add_argument("--force", action="store_true")
    _format_args(p)  # the snippet is Python either way; json changes how errors are printed
    p.set_defaults(fn=cmd_snippet)
    p = sub.add_parser("env", help="installed nautilus_trader and the names used")
    _format_args(p)
    p.set_defaults(fn=cmd_env)
    return ap


def _emit_error(e: QtNautilusError, json_out: bool) -> int:
    """As `queuetruth.cli._emit_error`: the `qt.error` document on stdout, or `<code>: <message>` on stderr."""
    if json_out:
        print(json.dumps(e.to_document(), ensure_ascii=False))
    else:
        print(f"error: {e}", file=sys.stderr)
        if e.hint:
            print(f"hint: {e.hint}", file=sys.stderr)
    return e.exit_code


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    json_out = getattr(a, "format", "text") == "json"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", CalibrationWarning)
        try:
            code = int(a.fn(a))
        except QtNautilusError as e:
            code = _emit_error(e, json_out)
        except KeyboardInterrupt:
            code = _emit_error(QtNautilusError("QT-USAGE-001", "interrupted"), json_out)
        except Exception as e:  # an unexpected failure is a bug: QT-ENGINE-001, exit 2 (never 1, which means refused)
            code = _emit_error(QtNautilusError("QT-ENGINE-001", f"{type(e).__name__}: {e}"), json_out)
        sys.stdout.flush()
        for w in caught:
            if issubclass(w.category, CalibrationWarning):
                print(f"warning: {w.message}", file=sys.stderr)
            else:
                warnings.showwarning(w.message, w.category, w.filename, w.lineno)
    return code if code in (0, 1, 2) else EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
