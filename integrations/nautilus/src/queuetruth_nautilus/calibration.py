"""Reading a queuetruth calibration for NautilusTrader (stdlib only; nautilus_trader is not imported here).

Two inputs are accepted:

- a `qt.calibration_result` JSON document (`calibration.json` of `queuetruth calibrate nt-touch` or
  `calibrate-live`, `fit.json`, or a directory holding one). This is the full record: estimate, events, train and test
  windows, data sources, held-out scores and the structured trust verdict.
- the text `queuetruth export nautilus --calibration ... --out FILE` writes (SPEC 5.10 layout). It carries the
  run id, the fitted values (`prob_fill_on_limit` to 6 significant digits, the latency p50s), whether the fit was
  queue-gated and, for a forced export, the `# !! NOT VALIDATED` reasons; held-out scores are not in it.

Mapping (the same one `queuetruth export nautilus` uses):

- `prob_fill_on_limit` = the estimate of an NT touch family (`nt_prob_touch`, `nt_queue_l2_prob_touch`); any other
  family fits no NautilusTrader fill probability.
- `queue_position` = True for `nt_queue_l2_prob_touch` (the fit counted only touches with the L2 queue exhausted),
  False for `nt_prob_touch` (NautilusTrader's touch model).
- latency: `insert` = p50 entry, `update` = p50 amend, `cancel` = p50 cancel one-way latency in ns, each only when it
  has samples.

Trust: the structured `trust` field (schema 1.1) is read first, then the `trust: verdict:` warning line (results
written before the field existed). A result with neither is assessed with `queuetruth.export.trust` when queuetruth is
installed; otherwise it is treated as refused (`no_trust_verdict`), so an unassessed fit never passes silently.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from queuetruth_nautilus.errors import QtNautilusError

SCHEMA = "qt.calibration_result"
#: Families whose estimate is NautilusTrader's `prob_fill_on_limit`.
NT_FAMILIES = ("nt_prob_touch", "nt_queue_l2_prob_touch")
#: The family fitted with NautilusTrader's L2 queue tracking (`queue_position=True`).
QUEUE_GATED = "nt_queue_l2_prob_touch"
#: NautilusTrader's default `prob_fill_on_limit`.
NT_DEFAULT_P = 1.0

TrustSource = Literal["structured", "warnings", "snippet", "queuetruth", "none"]


@dataclass(frozen=True)
class Latency:
    """Fitted one-way latencies in ns (p50); None for a component without samples."""

    insert_ns: int | None
    update_ns: int | None
    cancel_ns: int | None
    clock_sync_assumed: bool = True

    @property
    def fitted(self) -> bool:
        """True when at least one component has samples."""
        return any(v is not None for v in (self.insert_ns, self.update_ns, self.cancel_ns))


@dataclass(frozen=True)
class Trust:
    """The calibration's export verdict (`exportable` or `refused`) and where it was read from."""

    verdict: Literal["exportable", "refused"]
    reasons: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    #: `{horizon_ns: better|tie|worse|mixed|n/a}` (empty when not recorded, e.g. for a snippet).
    beats_default: dict[int, str] = field(default_factory=dict)
    source: TrustSource = "structured"
    #: Human-readable reason texts (snippet `# !!` lines or queuetruth's assessment), token -> text.
    texts: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.verdict == "exportable"


@dataclass(frozen=True)
class HeldOutScores:
    """Held-out metrics of one model per horizon (ns): Brier score, log loss, fill-rate bias (fraction)."""

    model_id: str
    brier: dict[int, float]
    logloss: dict[int, float]
    fill_rate_bias: dict[int, float]


@dataclass(frozen=True)
class NautilusCalibration:
    """What a queuetruth calibration says about a NautilusTrader venue. Build it with `load_calibration`."""

    run_id: str
    kind: str
    family: str
    #: queuetruth's fit mode (`faithful` describes NautilusTrader); `unknown` for an export text that does not name it
    mode: str
    #: Fitted `prob_fill_on_limit` in [0, 1], None when the calibration fits none.
    prob_fill_on_limit: float | None
    #: True when the fit used NautilusTrader's L2 queue tracking (`nt_queue_l2_prob_touch`).
    queue_position: bool
    latency: Latency | None
    trust: Trust
    n_events: int | None
    n_shadows: int | None
    ci_bootstrap: tuple[float | None, float | None] | None
    train: str
    test: str
    data_sources: tuple[dict[str, Any], ...]
    held_out: HeldOutScores | None
    baseline_held_out: HeldOutScores | None
    source_path: str
    source_format: Literal["calibration_result", "nautilus_snippet"]
    queuetruth_version: str | None = None

    @property
    def fits_fill_model(self) -> bool:
        return self.prob_fill_on_limit is not None

    def summary(self) -> dict[str, Any]:
        """JSON-able summary (stable key order)."""

        def ho(h: HeldOutScores | None) -> dict[str, Any] | None:
            if h is None:
                return None
            return {
                "model_id": h.model_id,
                "brier": {str(k): v for k, v in sorted(h.brier.items())},
                "logloss": {str(k): v for k, v in sorted(h.logloss.items())},
                "fill_rate_bias": {str(k): v for k, v in sorted(h.fill_rate_bias.items())},
            }

        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "family": self.family,
            "mode": self.mode,
            "prob_fill_on_limit": self.prob_fill_on_limit,
            "queue_position": self.queue_position,
            "latency": None
            if self.latency is None
            else {
                "insert_latency_nanos": self.latency.insert_ns,
                "update_latency_nanos": self.latency.update_ns,
                "cancel_latency_nanos": self.latency.cancel_ns,
                "clock_sync_assumed": self.latency.clock_sync_assumed,
            },
            "trust": {
                "verdict": self.trust.verdict,
                "reasons": list(self.trust.reasons),
                "flags": list(self.trust.flags),
                "beats_default": {str(k): v for k, v in sorted(self.trust.beats_default.items())},
                "source": self.trust.source,
            },
            "n_events": self.n_events,
            "n_shadows": self.n_shadows,
            "ci_bootstrap": None if self.ci_bootstrap is None else list(self.ci_bootstrap),
            "train": self.train,
            "test": self.test,
            "data_sources": [dict(d) for d in self.data_sources],
            "held_out": ho(self.held_out),
            "baseline_held_out": ho(self.baseline_held_out),
            "source_path": self.source_path,
            "source_format": self.source_format,
            "queuetruth_version": self.queuetruth_version,
        }


# ---- helpers ------------------------------------------------------------------------------------------------------


def _bad(path: Path, msg: str) -> QtNautilusError:
    return QtNautilusError("QT-INPUT-002", f"{path}: {msg}")


def family_key(family: str) -> str:
    """Normalized family name (`hbt_` prefix dropped, lower case), as queuetruth's export does."""
    f = family.strip().lower()
    if f.startswith("hbt_"):
        f = f[4:]
    if f == "databento_mc":
        f = "databento"
    return f


def _ranges(idx: list[int]) -> str:
    xs = sorted(set(idx))
    out: list[str] = []
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[j + 1] == xs[j] + 1:
            j += 1
        out.append(str(xs[i]) if i == j else f"{xs[i]}-{xs[j]}")
        i = j + 1
    return ",".join(out)


def window_text(w: Any) -> str:
    """`2019-12-30` | `2019-12-30 blocks 0-3/5` | `blocks 4/5` | `all data` (queuetruth's wording)."""
    if not isinstance(w, dict):
        return "all data"
    dates = w.get("dates") or []
    blocks = w.get("blocks")
    parts = []
    if dates:
        parts.append(",".join(str(d) for d in dates))
    if isinstance(blocks, dict) and blocks.get("index") is not None:
        parts.append(f"blocks {_ranges([int(i) for i in blocks['index']])}/{int(blocks['n_blocks'])}")
    return " ".join(parts) if parts else "all data"


def _p50(q: Any) -> int | None:
    if not isinstance(q, dict) or not q.get("n"):
        return None
    v = q.get("p50")
    if v is None or not isinstance(v, int | float) or not math.isfinite(v):
        return None
    return max(0, round(v))


def _hmap(path: Path, d: Any, name: str) -> dict[int, float]:
    if not isinstance(d, dict):
        raise _bad(path, f"{name}: expected an object of horizon -> number")
    out: dict[int, float] = {}
    for k, v in d.items():
        try:
            out[int(k)] = float(v)
        except (TypeError, ValueError) as e:
            raise _bad(path, f"{name}[{k!r}]: not a number ({v!r})") from e
    return out


def _held_out(path: Path, d: Any, name: str) -> HeldOutScores | None:
    if not d:
        return None
    if not isinstance(d, dict):
        raise _bad(path, f"{name}: expected an object")
    model_id, h = next(iter(d.items()))
    if not isinstance(h, dict):
        raise _bad(path, f"{name}.{model_id}: expected an object")
    return HeldOutScores(
        model_id=str(model_id),
        brier=_hmap(path, h.get("brier", {}), f"{name}.brier"),
        logloss=_hmap(path, h.get("logloss", {}), f"{name}.logloss"),
        fill_rate_bias=_hmap(path, h.get("fill_rate_bias", {}), f"{name}.fill_rate_bias"),
    )


_VERDICT_LINE = re.compile(r"^trust: verdict: (exportable|refused)(?: \(([^)]*)\))?\s*$")


def _str_list(path: Path, v: Any, name: str) -> tuple[str, ...]:
    if v is None:
        return ()
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise _bad(path, f"{name}: expected a list of strings, got {v!r}")
    return tuple(v)


def _beats_default(path: Path, v: Any) -> dict[int, str]:
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise _bad(path, f"trust.beats_default: expected an object of horizon_ns -> label, got {v!r}")
    out: dict[int, str] = {}
    for k, label in v.items():
        if not (isinstance(k, str) and re.fullmatch(r"[0-9]+", k)):
            raise _bad(path, f"trust.beats_default: key {k!r} is not a horizon in integer ns")
        if not isinstance(label, str):
            raise _bad(path, f"trust.beats_default[{k!r}]: expected a string, got {label!r}")
        out[int(k)] = label
    return out


def _trust_from_doc(path: Path, doc: dict[str, Any]) -> Trust:
    t = doc.get("trust")
    if isinstance(t, dict):
        # the structured field wins: an unknown verdict is an error, never a fall-through to older text
        if t.get("verdict") not in ("exportable", "refused"):
            raise _bad(path, f"trust.verdict: expected 'exportable' or 'refused', got {t.get('verdict')!r}")
        return Trust(
            verdict=t["verdict"],
            reasons=_str_list(path, t.get("reasons"), "trust.reasons"),
            flags=_str_list(path, t.get("flags"), "trust.flags"),
            beats_default=_beats_default(path, t.get("beats_default")),
            source="structured",
        )
    if t is not None:
        raise _bad(path, "trust: expected an object or null")
    for w in reversed([w for w in doc.get("warnings") or [] if isinstance(w, str)]):
        m = _VERDICT_LINE.match(w)
        if m:
            reasons = tuple(r.strip() for r in (m.group(2) or "").split(",") if r.strip())
            return Trust(verdict=m.group(1), reasons=reasons, source="warnings")  # type: ignore[arg-type]
    return _trust_via_queuetruth(doc)


def _trust_via_queuetruth(doc: dict[str, Any]) -> Trust:
    """A result without any recorded verdict: queuetruth's own assessment when it is installed, else refused."""
    try:
        from queuetruth.export import trust as qtrust  # type: ignore[import-not-found]
        from queuetruth.schemas.models import CalibrationResult  # type: ignore[import-not-found]
    except ImportError:
        return Trust(
            verdict="refused",
            reasons=("no_trust_verdict",),
            source="none",
            texts={
                "no_trust_verdict": "the result records no trust verdict and queuetruth is not installed to assess it"
            },
        )
    a = qtrust.assess(CalibrationResult.model_validate(doc))
    reasons = tuple(k for k, _ in a.reasons)
    return Trust(
        verdict="exportable" if a.ok else "refused",
        reasons=reasons,
        flags=tuple(sorted(a.flags)),
        beats_default={h.horizon_ns: h.label for h in a.horizons},
        source="queuetruth",
        texts=dict(a.reasons),
    )


def _is_num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _ci(path: Path, v: Any) -> tuple[float | None, float | None] | None:
    """`ci_bootstrap`: null or a 2-item list of number-or-null."""
    if v is None:
        return None
    if not isinstance(v, list) or len(v) != 2 or not all(x is None or _is_num(x) for x in v):
        raise _bad(path, f"ci_bootstrap: expected null or [low, high] of numbers or nulls, got {v!r}")
    return (None if v[0] is None else float(v[0]), None if v[1] is None else float(v[1]))


def _window(path: Path, w: Any, name: str) -> str:
    try:
        return window_text(w)
    except (KeyError, TypeError, ValueError) as e:
        raise _bad(path, f"{name}: malformed window {w!r}") from e


def _count(path: Path, v: Any, name: str) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise _bad(path, f"{name}: expected a non-negative integer or null, got {v!r}")
    return v


# ---- calibration_result ---------------------------------------------------------------------------------------------


def from_document(doc: dict[str, Any], path: str | Path = "<document>") -> NautilusCalibration:
    """A `qt.calibration_result` document (already parsed JSON) as a `NautilusCalibration`."""
    p = Path(path)
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        got = doc.get("schema") if isinstance(doc, dict) else type(doc).__name__
        raise _bad(p, f"not a {SCHEMA} (schema {got!r})")
    if doc.get("schema_version") != 1:
        raise _bad(p, f"{SCHEMA} schema_version {doc.get('schema_version')!r} is not supported (expected 1)")
    for k in ("run_id", "kind", "family"):
        if not isinstance(doc.get(k), str) or not doc[k]:
            raise _bad(p, f"missing or empty field {k!r}")
    fam = family_key(doc["family"])
    est = doc.get("estimate")
    prob: float | None = None
    if fam in NT_FAMILIES and est is not None:
        if not isinstance(est, int | float) or isinstance(est, bool):
            raise _bad(p, f"estimate: expected a number, got {est!r}")
        if math.isfinite(est):
            if not 0.0 <= est <= 1.0:
                raise _bad(p, f"estimate {est!r} of {fam} is not a probability in [0, 1]")
            prob = float(est)
    lat = doc.get("latency")
    latency = None
    if isinstance(lat, dict):
        latency = Latency(
            insert_ns=_p50(lat.get("entry_ns")),
            update_ns=_p50(lat.get("amend_ns")),
            cancel_ns=_p50(lat.get("cancel_ns")),
            clock_sync_assumed=bool(lat.get("clock_sync_assumed", True)),
        )
    elif lat is not None:
        raise _bad(p, "latency: expected an object or null")
    ci_t = _ci(p, doc.get("ci_bootstrap"))
    ds = doc.get("data_sources") or []
    if not isinstance(ds, list):
        raise _bad(p, "data_sources: expected a list")
    return NautilusCalibration(
        run_id=doc["run_id"],
        kind=doc["kind"],
        family=fam,
        mode=str(doc.get("mode") or "faithful"),
        prob_fill_on_limit=prob,
        queue_position=fam == QUEUE_GATED,
        latency=latency,
        trust=_trust_from_doc(p, doc),
        n_events=_count(p, doc.get("n_events"), "n_events"),
        n_shadows=_count(p, doc.get("n_shadows"), "n_shadows"),
        ci_bootstrap=ci_t,
        train=_window(p, doc.get("train"), "train"),
        test=_window(p, doc.get("test"), "test"),
        data_sources=tuple(dict(d) for d in ds if isinstance(d, dict)),
        held_out=_held_out(p, doc.get("held_out"), "held_out"),
        baseline_held_out=_held_out(p, doc.get("baseline_held_out"), "baseline_held_out"),
        source_path=str(path),
        source_format="calibration_result",
    )


# ---- export snippet -----------------------------------------------------------------------------------------------

_HEAD = re.compile(r"^# queuetruth (\S+) \| calibration ([0-9A-Za-z_-]+) \|")
_FAM = re.compile(r"^# (\S+) fitted on (.+?); held-out (.+?); data: (.+)$")
_FIT = re.compile(r"^# fit: .*? \((\w+)\); (\d+) events from (\d+) shadows; bootstrap CI \[([^,\]]+), ([^\]]+)\]")
_FILL = re.compile(r"^fill_model = \w+\(prob_fill_on_limit=([^,)\s]+)")
_LAT = re.compile(r"^latency_model = \w+\((.*)\)\s*$")
_KW = re.compile(r"(\w+)=(-?\d+)")
_GATED = re.compile(r"queue-gated \((yes|no)\)")
_REASON = re.compile(r"^# !!   - ([a-z_]+): (.*)$")
#: the export names the default it compared against with the fit's mode: `nt_prob_touch[mode=faithful;p=1]`
_MODE = re.compile(r"^# held-out: .*?\[mode=(\w+)[;\]]")
#: `mode` of a snippet that does not name its baseline (the export text has no mode field)
MODE_UNKNOWN = "unknown"


def _num(s: str) -> float | None:
    s = s.strip()
    if s in ("-", ""):
        return None
    return float(s)


def from_snippet(text: str, path: str | Path = "<snippet>") -> NautilusCalibration:
    """The text of `queuetruth export nautilus` as a `NautilusCalibration` (held-out scores are not in it)."""
    p = Path(path)
    lines = text.splitlines()
    head = _HEAD.match(lines[0]) if lines else None
    if head is None or "NautilusTrader" not in lines[0]:
        raise _bad(p, "not a `queuetruth export nautilus` snippet (first line must be the queuetruth header)")
    version, run_id = head.group(1), head.group(2)
    family = kind = None
    train = test = "all data"
    data_text = None
    n_events = n_shadows = None
    ci: tuple[float | None, float | None] | None = None
    prob = None
    gated = None
    lat_kw: dict[str, int] = {}
    reasons: dict[str, str] = {}
    not_validated = False
    mode = MODE_UNKNOWN
    for ln in lines[1:]:
        if m := _FAM.match(ln):
            family, train, test, data_text = m.group(1), m.group(2), m.group(3), m.group(4)
        elif m := _FIT.match(ln):
            kind, n_events, n_shadows = m.group(1), int(m.group(2)), int(m.group(3))
            ci = (_num(m.group(4)), _num(m.group(5)))
        elif m := _FILL.match(ln):
            try:
                prob = float(m.group(1))
            except ValueError as e:
                raise _bad(p, f"prob_fill_on_limit {m.group(1)!r} is not a number") from e
            if not (math.isfinite(prob) and 0.0 <= prob <= 1.0):
                raise _bad(p, f"prob_fill_on_limit {prob!r} is not a probability in [0, 1]")
        elif m := _LAT.match(ln):
            lat_kw = {k: int(v) for k, v in _KW.findall(m.group(1))}
            if any(v < 0 for v in lat_kw.values()):
                raise _bad(p, f"negative latency in {ln.strip()!r}")
        elif m := _REASON.match(ln):
            reasons[m.group(1)] = m.group(2)
        elif m := _MODE.match(ln):
            mode = m.group(1)
        if "!! NOT VALIDATED" in ln:
            not_validated = True
        if m := _GATED.search(ln):
            gated = m.group(1) == "yes"
    if family is None:
        raise _bad(p, "snippet has no `# <family> fitted on ...` line")
    fam = family_key(family)
    if gated is None:
        gated = fam == QUEUE_GATED
    latency = None
    if lat_kw:
        if lat_kw.get("base_latency_nanos", 0) != 0:
            raise _bad(p, "latency_model: queuetruth exports base_latency_nanos=0; this snippet was edited")
        latency = Latency(
            insert_ns=lat_kw.get("insert_latency_nanos"),
            update_ns=lat_kw.get("update_latency_nanos"),
            cancel_ns=lat_kw.get("cancel_latency_nanos", lat_kw.get("delete_latency_nanos")),
        )
    if not_validated:
        trust = Trust(
            verdict="refused",
            reasons=tuple(reasons) or ("not_validated",),
            source="snippet",
            texts=reasons or {"not_validated": "the snippet carries a NOT VALIDATED header"},
        )
    else:
        trust = Trust(verdict="exportable", source="snippet")
    return NautilusCalibration(
        run_id=run_id,
        kind=kind or ("latency" if fam == "latency" else "nt_touch" if fam in NT_FAMILIES else "unknown"),
        family=fam,
        mode=mode,
        prob_fill_on_limit=prob if fam in NT_FAMILIES else None,
        queue_position=bool(gated) and fam in NT_FAMILIES,
        latency=latency,
        trust=trust,
        n_events=n_events,
        n_shadows=n_shadows,
        ci_bootstrap=ci,
        train=train,
        test=test,
        data_sources=({"text": data_text},) if data_text else (),
        held_out=None,
        baseline_held_out=None,
        source_path=str(path),
        source_format="nautilus_snippet",
        queuetruth_version=version,
    )


# ---- entry point --------------------------------------------------------------------------------------------------


def load_calibration(path: str | Path) -> NautilusCalibration:
    """Reads a `qt.calibration_result` JSON file (or a directory with `calibration.json` / `fit.json`) or a
    `queuetruth export nautilus` snippet. Raises `QT-INPUT-001` (missing or unreadable) or `QT-INPUT-002` (not one of
    these). The trust verdict is recorded, not enforced: `fill_model` and `venue_options` refuse a refused fit."""
    p = Path(path)
    if p.is_dir():
        for name in ("calibration.json", "fit.json"):
            if (p / name).is_file():
                p = p / name
                break
        else:
            raise QtNautilusError("QT-INPUT-001", f"{p}: no calibration.json or fit.json in this directory")
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise QtNautilusError("QT-INPUT-001", f"{p}: calibration not found") from e
    except (OSError, UnicodeDecodeError) as e:
        raise QtNautilusError("QT-INPUT-001", f"{p}: unreadable ({e})") from e
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as e:
            raise _bad(p, f"invalid JSON ({e})") from e
        return from_document(doc, p)
    if stripped.startswith("# queuetruth"):
        return from_snippet(stripped, p)
    raise _bad(p, "neither a qt.calibration_result JSON document nor a `queuetruth export nautilus` snippet")
