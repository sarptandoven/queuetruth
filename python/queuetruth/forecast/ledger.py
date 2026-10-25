"""Forecast ledger: an append-only, hash-chained JSON Lines file of forecasts and resolutions (timing truth).

Each line is one `qt.forecast_entry` in canonical JSON (`canonical_json`, byte-identical to
`queuetruth.schemas.runid.canonical_json`): RFC 8785 (keys sorted by UTF-16 code units, no whitespace, floats in
ECMAScript form) except that integers are written exactly as decimal digits. RFC 8785 reads every number as an
IEEE double, and `recorded_at_ns` (about 1.8e18) is above 2^53, so a stock JCS library would round it and compute
other hashes; a third-party verifier must keep integers exact (Python's `json` does; in JavaScript use a BigInt-aware
parser). `forecast verify` is the reference verifier:

    payload_sha256 = sha256(canonical_json(payload))
    entry_sha256   = sha256(canonical_json(entry without entry_sha256))
    prev_sha256    = entry_sha256 of entry seq - 1 (64 zeros for seq 0)

so changing, dropping, reordering or inserting any entry breaks every later hash. `recorded_at_ns` comes from the
writer's UTC clock and never goes backwards along the chain (`clock: system`); a time supplied by the writer is
kept as `clock: declared` and `score` does not trust it unless told to. Appends hold an exclusive `flock` on the
file (POSIX), so two writers cannot fork the chain; readers (`verify_ledger`, `forecast score`) hold a shared lock, so
they never see half of an append.

The local chain proves order and integrity, not absolute time: anyone holding the file can rewrite it from the
first changed entry onward. Publishing `commitment()` (a `qt.forecast_commitment`: the head hash, no forecast
content) to a third party or wrapping it in a timestamp certificate before the questions resolve fixes the
entries up to that head in time; `check_commitment` later shows that the ledger still contains them unchanged.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import secrets
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from pydantic import ValidationError

from queuetruth._version import __version__
from queuetruth.errors import QtError
from queuetruth.forecast.io import parse_ts
from queuetruth.forecast.schemas import (
    ForecastCommitment,
    ForecastEntry,
    ForecastPayload,
    LedgerProblem,
    ResolutionPayload,
)
from queuetruth.schemas.runid import _format_float, _utf16_key

GENESIS = "0" * 64
CHAIN_RULE = (
    "entry_sha256 = sha256(canonical_json(entry - entry_sha256)); prev_sha256 = entry_sha256 of seq - 1 "
    "(64 zeros for seq 0); payload_sha256 = sha256(canonical_json(payload)); canonical_json = RFC 8785 with "
    "integers written exactly"
)
#: Largest relative error of a categorical forecast's sum that `normalize_forecast` repairs by renormalising.
NORMALISE_TOL = 1e-3

Payload = ForecastPayload | ResolutionPayload


_encode_str = json.encoder.encode_basestring  # escapes exactly as RFC 8785 (checked against runid in the tests)


def canonical_json(value: Any) -> str:
    """RFC 8785 canonical JSON, identical to `queuetruth.schemas.runid.canonical_json` (sorted keys by UTF-16 code
    units, no whitespace, ECMAScript numbers) with strings escaped by the C JSON encoder; ledgers hash every entry
    twice, so this is on the hot path."""
    if isinstance(value, str):
        return _encode_str(value)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _format_float(value)
    if isinstance(value, dict):
        for k in value:
            if not isinstance(k, str):
                raise TypeError(f"object keys must be strings, got {k!r}")
        items = sorted(value.items(), key=lambda kv: _utf16_key(kv[0]))
        return "{" + ",".join(_encode_str(k) + ":" + canonical_json(v) for k, v in items) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    raise TypeError(f"value of type {type(value).__name__} is not JSON")


def sha256_hex(text: str) -> str:
    """sha256 of the UTF-8 text, as hex."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def payload_digest(payload: Payload | dict[str, Any]) -> str:
    """`payload_sha256` of a payload."""
    doc = payload.model_dump(mode="json") if not isinstance(payload, dict) else payload
    return sha256_hex(canonical_json(doc))


def entry_digest(entry: dict[str, Any]) -> str:
    """`entry_sha256` of an entry document (its own `entry_sha256` key, if present, is ignored)."""
    return sha256_hex(canonical_json({k: v for k, v in entry.items() if k != "entry_sha256"}))


# ---- input normalisation ------------------------------------------------------------------------------------------


def _prob(v: Any, where: str) -> float:
    if isinstance(v, bool) or v is None:
        raise QtError("QT-INPUT-002", f"{where}: expected a probability, got {v!r}")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise QtError("QT-INPUT-002", f"{where}: expected a probability, got {v!r}") from None
    if not (math.isfinite(x) and 0.0 <= x <= 1.0):
        raise QtError("QT-INPUT-002", f"{where}: probability {x!r} is outside [0, 1]")
    return x


def _floats(v: Any, where: str) -> list[float]:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            raise QtError("QT-INPUT-002", f"{where}: expected a list of numbers") from None
    if not isinstance(v, (list, tuple)) or not v:
        raise QtError("QT-INPUT-002", f"{where}: expected a non-empty list of numbers")
    out = []
    for x in v:
        if isinstance(x, bool):
            raise QtError("QT-INPUT-002", f"{where}: expected numbers, got {x!r}")
        try:
            f = float(x)
        except (TypeError, ValueError):
            raise QtError("QT-INPUT-002", f"{where}: expected numbers, got {x!r}") from None
        if not math.isfinite(f):
            raise QtError("QT-INPUT-002", f"{where}: non-finite value {x!r}")
        out.append(f)
    return out


def _present(row: dict[str, Any], key: str) -> bool:
    v = row.get(key)
    return v is not None and not (isinstance(v, float) and math.isnan(v)) and v != ""


def _no_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name} (NaN and Infinity are not JSON)")


def loads_strict(text: str | bytes) -> Any:
    """`json.loads` that refuses NaN and Infinity (Python's `json` accepts them by default)."""
    return json.loads(text, parse_constant=_no_constant)


def normalize_forecast(row: dict[str, Any], where: str, forecaster: str | None = None) -> ForecastPayload:
    """A `ForecastPayload` from one input row.

    Columns: `question_id`; `forecaster` (or the default); one of `p` (binary), `probs` (categorical: an object
    label -> probability; a sum within 1e-3 of 1 is renormalised), `mean` + `sd` (normal), `quantiles` (an object
    level -> value, or `levels` + `values`), `samples` (a list); optional `kind`, `as_of` (timestamp) and `nonce`
    (64 hex; random when absent).
    """
    qid = row.get("question_id")
    if not _present(row, "question_id"):
        raise QtError("QT-INPUT-002", f"{where}: question_id is required")
    who = row.get("forecaster") if _present(row, "forecaster") else forecaster
    if who is None or str(who) == "":
        raise QtError("QT-INPUT-002", f"{where}: forecaster is required (column or --forecaster)")
    shapes = [k for k in ("p", "probs", "mean", "quantiles", "levels", "samples") if _present(row, k)]
    if "levels" in shapes and "quantiles" in shapes:
        raise QtError("QT-INPUT-002", f"{where}: give quantiles or levels + values, not both")
    kinds = {"p": "binary", "probs": "categorical", "mean": "continuous", "quantiles": "continuous"}
    kinds |= {"levels": "continuous", "samples": "continuous"}
    found = sorted({kinds[s] for s in shapes})
    if len(shapes) != 1:
        raise QtError(
            "QT-INPUT-002",
            f"{where}: exactly one of p, probs, mean+sd, quantiles, samples is required (got {shapes or 'none'})",
        )
    kind = found[0]
    if _present(row, "kind") and row["kind"] != kind:
        raise QtError("QT-INPUT-002", f"{where}: kind {row['kind']!r} does not match the forecast given ({kind})")
    body: dict[str, Any]
    shape = shapes[0]
    if shape == "p":
        body = {"kind": "binary", "p": _prob(row["p"], f"{where}: p")}
    elif shape == "probs":
        probs = row["probs"]
        if isinstance(probs, str):
            try:
                probs = json.loads(probs)
            except json.JSONDecodeError:
                raise QtError("QT-INPUT-002", f"{where}: probs must be a JSON object") from None
        if not isinstance(probs, dict) or len(probs) < 2:
            raise QtError("QT-INPUT-002", f"{where}: probs must map two or more outcome labels to probabilities")
        vals = {str(k): _prob(v, f"{where}: probs[{k!r}]") for k, v in probs.items()}
        total = sum(vals.values())
        if abs(total - 1.0) > NORMALISE_TOL:
            raise QtError("QT-INPUT-002", f"{where}: probs sum to {total:.6g}, not 1")
        body = {"kind": "categorical", "probs": {k: v / total for k, v in sorted(vals.items())}}
    elif shape == "mean":
        if not _present(row, "sd"):
            raise QtError("QT-INPUT-002", f"{where}: a normal forecast needs mean and sd")
        mean, sd = _floats([row["mean"], row["sd"]], f"{where}: mean/sd")
        if sd <= 0:
            raise QtError("QT-INPUT-002", f"{where}: sd must be positive, got {sd}")
        body = {"kind": "continuous", "dist": {"family": "normal", "mean": mean, "sd": sd}}
    elif shape in ("quantiles", "levels"):
        if shape == "quantiles":
            q = row["quantiles"]
            if isinstance(q, str):
                try:
                    q = json.loads(q)
                except json.JSONDecodeError:
                    raise QtError("QT-INPUT-002", f"{where}: quantiles must be a JSON object") from None
            if not isinstance(q, dict) or not q:
                raise QtError("QT-INPUT-002", f"{where}: quantiles must map levels to values")
            pairs = sorted((_floats([k], f"{where}: level")[0], v) for k, v in q.items())
            levels = [a for a, _ in pairs]
            values = _floats([b for _, b in pairs], f"{where}: quantile values")
        else:
            levels = _floats(row["levels"], f"{where}: levels")
            values = _floats(row.get("values"), f"{where}: values")
        body = {"kind": "continuous", "dist": {"family": "quantiles", "levels": levels, "values": values}}
    else:
        body = {"kind": "continuous", "dist": {"family": "samples", "samples": _floats(row["samples"], where)}}
    as_of = parse_ts(row["as_of"], f"{where}: as_of") if _present(row, "as_of") else None
    nonce = str(row["nonce"]) if _present(row, "nonce") else secrets.token_hex(32)
    try:
        return ForecastPayload(question_id=str(qid), forecaster=str(who), forecast=body, as_of_ns=as_of, nonce=nonce)
    except ValidationError as e:
        raise QtError("QT-INPUT-002", f"{where}: {_first_error(e)}") from None


_YES = {"1", "yes", "true", "y"}
_NO = {"0", "no", "false", "n"}


def normalize_resolution(row: dict[str, Any], where: str) -> ResolutionPayload:
    """A `ResolutionPayload` from one input row.

    Columns: `question_id`, `outcome`, `resolved_at` (timestamp); optional `kind`, `outcomes` (list of labels;
    implies categorical), `ordered`, `info_cutoff` (timestamp, not after `resolved_at`), `void`, `group`, `source`.
    Without `kind`: categorical when `outcomes` is given, binary for 0/1/yes/no/true/false, else continuous.
    """
    if not _present(row, "question_id"):
        raise QtError("QT-INPUT-002", f"{where}: question_id is required")
    void = bool(row.get("void")) if not isinstance(row.get("void"), str) else row["void"].lower() in _YES
    resolved_at = parse_ts(row.get("resolved_at"), f"{where}: resolved_at")
    cutoff = parse_ts(row["info_cutoff"], f"{where}: info_cutoff") if _present(row, "info_cutoff") else None
    if cutoff is not None and cutoff > resolved_at:
        raise QtError("QT-INPUT-002", f"{where}: info_cutoff is after resolved_at")
    outcomes = row.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            outcomes = [s.strip() for s in outcomes.split("|") if s.strip()]
    if outcomes is not None:
        if not isinstance(outcomes, list) or len(outcomes) < 2:
            raise QtError("QT-INPUT-002", f"{where}: outcomes must list two or more labels")
        outcomes = [str(x) for x in outcomes]
        if len(set(outcomes)) != len(outcomes):
            raise QtError("QT-INPUT-002", f"{where}: outcomes has duplicate labels")
    raw = row.get("outcome")
    kind = row.get("kind") if _present(row, "kind") else None
    if kind is None:
        if outcomes is not None:
            kind = "categorical"
        elif (
            isinstance(raw, bool)
            or str(raw).strip().lower() in _YES | _NO
            or (isinstance(raw, (int, float)) and raw in (0, 1))
        ):
            kind = "binary"
        else:
            kind = "continuous"
    outcome: float | str | None
    if void and not _present(row, "outcome"):
        outcome = None
    elif kind == "binary":
        t = str(raw).strip().lower() if not isinstance(raw, bool) else ("1" if raw else "0")
        if t in ("1.0", "0.0"):
            t = t[0]
        if t not in _YES | _NO:
            raise QtError("QT-INPUT-002", f"{where}: binary outcome must be 0/1, yes/no or true/false, got {raw!r}")
        outcome = 1.0 if t in _YES else 0.0
    elif kind == "categorical":
        if outcomes is None:
            raise QtError("QT-INPUT-002", f"{where}: a categorical question needs outcomes")
        if str(raw) not in outcomes:
            raise QtError("QT-INPUT-002", f"{where}: outcome {raw!r} is not one of outcomes {outcomes}")
        outcome = str(raw)
    elif kind == "continuous":
        outcome = _floats([raw], f"{where}: outcome")[0]
    else:
        raise QtError("QT-INPUT-002", f"{where}: unknown kind {kind!r}")
    ordered = row.get("ordered")
    ordered_b = (str(ordered).lower() in _YES) if ordered is not None else False
    try:
        return ResolutionPayload(
            question_id=str(row["question_id"]),
            kind=kind,
            outcome=outcome,
            outcomes=outcomes,
            ordered=ordered_b,
            resolved_at_ns=resolved_at,
            info_cutoff_ns=cutoff,
            void=void,
            group=str(row["group"]) if _present(row, "group") else None,
            source=str(row["source"]) if _present(row, "source") else None,
        )
    except ValidationError as e:
        raise QtError("QT-INPUT-002", f"{where}: {_first_error(e)}") from None


def _first_error(e: ValidationError) -> str:
    err = e.errors()[0]
    loc = ".".join(str(x) for x in err.get("loc", ()))
    return f"{loc}: {err.get('msg')}" if loc else str(err.get("msg"))


# ---- the ledger ---------------------------------------------------------------------------------------------------


@dataclass
class Verification:
    """Result of reading and checking a ledger."""

    entries: list[ForecastEntry] = field(default_factory=list)
    problems: list[LedgerProblem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """No integrity problem."""
        return not self.problems


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[IO[bytes]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX: no inter-process lock
            fcntl = None  # type: ignore[assignment]
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield fh
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def verify_bytes(data: bytes) -> Verification:
    """Checks ledger content already read (see `verify_ledger`)."""
    v = Verification()
    if data and not data.endswith(b"\n"):
        v.problems.append(LedgerProblem(seq=None, line=data.count(b"\n") + 1, problem="truncated last line"))
    prev = GENESIS
    last_system: int | None = None
    seen: dict[str, int] = {}
    seq_expected = -1
    for lineno, raw in enumerate(data.split(b"\n"), start=1):
        if not raw.strip():
            continue
        seq_expected += 1
        try:
            doc = loads_strict(raw.decode("utf-8"))
            entry = ForecastEntry.model_validate(doc)
            digests = (entry_digest(doc), payload_digest(doc["payload"]))
        except (UnicodeDecodeError, ValueError, TypeError) as e:  # JSONDecodeError and ValidationError included
            msg = _first_error(e) if isinstance(e, ValidationError) else str(e)
            v.problems.append(LedgerProblem(seq=None, line=lineno, problem=f"not a qt.forecast_entry: {msg}"))
            prev = "invalid"
            continue
        problems = []
        if entry.seq != seq_expected:
            problems.append(f"seq {entry.seq}, expected {seq_expected}")
        if entry.prev_sha256 != prev:
            problems.append("prev_sha256 does not match the previous entry (chain broken)")
        if digests[0] != entry.entry_sha256:
            problems.append("entry_sha256 does not match the entry (entry changed)")
        if digests[1] != entry.payload_sha256:
            problems.append("payload_sha256 does not match the payload (payload changed)")
        if entry.clock == "system":
            if last_system is not None and entry.recorded_at_ns < last_system:
                problems.append("recorded_at_ns goes backwards on the system clock")
            last_system = max(entry.recorded_at_ns, last_system or entry.recorded_at_ns)
        if entry.payload_sha256 in seen:
            problems.append(f"duplicate payload of seq {seen[entry.payload_sha256]}")
        seen.setdefault(entry.payload_sha256, entry.seq)
        for p in problems:
            v.problems.append(LedgerProblem(seq=entry.seq, line=lineno, problem=p))
        prev = entry.entry_sha256
        v.entries.append(entry)
    return v


def read_ledger_bytes(path: str | Path) -> bytes:
    """The ledger's bytes, read under a shared lock (an append in progress finishes first)."""
    p = Path(path)
    if not p.is_file():
        raise QtError("QT-INPUT-001", f"{p}: no such ledger")
    with open(p, "rb") as fh:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX: no inter-process lock
            return fh.read()
        fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
        try:
            return fh.read()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def verify_ledger(path: str | Path) -> Verification:
    """Reads the ledger (shared lock) and checks every hash, link, sequence number and the system clock order."""
    return verify_bytes(read_ledger_bytes(path))


def commitment(entries: list[ForecastEntry]) -> ForecastCommitment:
    """The `qt.forecast_commitment` of the ledger head."""
    if not entries:
        raise QtError("QT-INPUT-002", "the ledger is empty: nothing to commit to")
    head = entries[-1]
    n_f = sum(1 for e in entries if e.payload.type == "forecast")
    return ForecastCommitment(
        ledger_id=entries[0].entry_sha256,
        head_seq=head.seq,
        head_sha256=head.entry_sha256,
        head_recorded_at_ns=head.recorded_at_ns,
        n_entries=len(entries),
        n_forecasts=n_f,
        n_resolutions=len(entries) - n_f,
        chain_rule=CHAIN_RULE,
        engine_version=__version__,
    )


def commitment_digest(c: ForecastCommitment) -> str:
    """sha256 of the commitment's canonical JSON: the value a timestamp certificate signs."""
    return sha256_hex(canonical_json(c.model_dump(mode="json")))


def check_commitment(entries: list[ForecastEntry], c: ForecastCommitment) -> list[str]:
    """Problems that keep `c` from vouching for `entries` (empty = every entry up to `c.head_seq` is unchanged)."""
    out = []
    if not entries:
        return ["the ledger is empty"]
    if entries[0].entry_sha256 != c.ledger_id:
        out.append("ledger_id differs: a different ledger, or entry 0 changed")
    if c.head_seq >= len(entries):
        out.append(f"the ledger has {len(entries)} entries; the commitment's head is seq {c.head_seq}")
    elif entries[c.head_seq].entry_sha256 != c.head_sha256:
        out.append(f"entry {c.head_seq} differs from the committed head: an entry at or before it changed")
    return out


def _check_appendable(v: Verification, path: Path) -> None:
    if not v.ok:
        first = v.problems[0]
        raise QtError(
            "QT-INPUT-002",
            f"{path}: ledger integrity check failed at line {first.line}: {first.problem}",
            "run `queuetruth forecast verify`; a broken ledger cannot be extended",
            {"problems": [p.model_dump() for p in v.problems[:20]]},
        )


def _body_key(pl: ForecastPayload) -> str:
    """A forecast without its nonce: what a re-run of the same file would commit again."""
    return canonical_json(pl.model_dump(mode="json", exclude={"nonce"}))


def append(
    path: str | Path,
    payloads: Iterable[Payload],
    *,
    declared_at_ns: int | Sequence[int | None] | None = None,
    allow_correction: bool = False,
    generated_nonce: Sequence[bool] | None = None,
    clock: Callable[[], int] = time.time_ns,
) -> list[ForecastEntry]:
    """Appends payloads as chained entries under an exclusive lock; returns the new entries.

    `declared_at_ns` (one time for every payload, or one per payload, None = system clock) records that time with
    `clock: declared` instead of reading `clock` (the system clock). Refused: a broken ledger (`QT-INPUT-002`), a
    payload already in the ledger (`QT-INPUT-003`), a second resolution of a question unless `allow_correction`
    (`QT-INPUT-003`), and a forecast whose nonce was generated (`generated_nonce[i]`) and whose content
    (forecaster, question, forecast, as_of) equals a forecast already in the ledger or earlier in the batch
    (`QT-INPUT-003`): re-running a commit of a file without nonces would otherwise append the same forecasts again
    under new nonces. Pass `generated_nonce=None` to allow such repeats.
    """
    p = Path(path)
    items = list(payloads)
    if declared_at_ns is None or isinstance(declared_at_ns, int):
        declared: list[int | None] = [declared_at_ns] * len(items)
    else:
        declared = list(declared_at_ns)
        if len(declared) != len(items):
            raise ValueError("declared_at_ns must have one entry per payload")
    if generated_nonce is not None and len(generated_nonce) != len(items):
        raise ValueError("generated_nonce must have one entry per payload")
    with _locked(p) as fh:
        fh.seek(0)
        v = verify_bytes(fh.read())
        _check_appendable(v, p)
        entries = v.entries
        seen = {e.payload_sha256 for e in entries}
        resolved = {e.payload.question_id for e in entries if e.payload.type == "resolution"}
        bodies = (
            {_body_key(e.payload) for e in entries if isinstance(e.payload, ForecastPayload)}
            if generated_nonce is not None and any(generated_nonce)
            else set()
        )
        last_system = max((e.recorded_at_ns for e in entries if e.clock == "system"), default=None)
        prev = entries[-1].entry_sha256 if entries else GENESIS
        seq = len(entries)
        new: list[ForecastEntry] = []
        lines: list[str] = []
        for idx, pl in enumerate(items):
            digest = payload_digest(pl)
            if isinstance(pl, ForecastPayload) and generated_nonce is not None and any(generated_nonce):
                key = _body_key(pl)
                if generated_nonce[idx] and key in bodies:
                    raise QtError(
                        "QT-INPUT-003",
                        f"repeated forecast: {pl.question_id} by {pl.forecaster} with the same content is already in "
                        f"{p} (this row has no nonce, so re-running the commit would count it twice)",
                        "pass --allow-repeat to record the same forecast again, or give each row a nonce",
                    )
                bodies.add(key)
            if digest in seen:
                who = getattr(pl, "forecaster", None)
                what = f"{pl.question_id}" + (f" by {who}" if who else "")
                raise QtError(
                    "QT-INPUT-003",
                    f"duplicate ledger payload: {what} (payload_sha256 {digest[:16]}...) is already in {p}",
                    "a forecast with a fresh nonce is a new forecast; the same payload is never committed twice",
                )
            if pl.type == "resolution":
                if pl.question_id in resolved and not allow_correction:
                    raise QtError(
                        "QT-INPUT-003",
                        f"duplicate resolution: question {pl.question_id!r} is already resolved in {p}",
                        "pass --correct to record a corrected resolution (it changes the outcome scored; a "
                        "correction can move the question's cutoff earlier, never later)",
                    )
                resolved.add(pl.question_id)
            seen.add(digest)
            if declared[idx] is not None:
                ts, how = int(declared[idx]), "declared"  # type: ignore[arg-type]
            else:
                now = int(clock())
                ts = now if last_system is None else max(now, last_system)
                last_system, how = ts, "system"
            doc = {
                "schema": "qt.forecast_entry",
                "schema_version": 1,
                "seq": seq,
                "prev_sha256": prev,
                "recorded_at_ns": ts,
                "clock": how,
                "payload_sha256": digest,
                "payload": pl.model_dump(mode="json"),
            }
            doc["entry_sha256"] = entry_digest(doc)
            entry = ForecastEntry.model_validate(doc)
            new.append(entry)
            lines.append(canonical_json(doc))
            prev = doc["entry_sha256"]
            seq += 1
        if lines:
            fh.seek(0, 2)
            fh.write(("\n".join(lines) + "\n").encode("utf-8"))
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
    return new


__all__ = [
    "CHAIN_RULE",
    "GENESIS",
    "Verification",
    "append",
    "canonical_json",
    "check_commitment",
    "commitment",
    "commitment_digest",
    "entry_digest",
    "loads_strict",
    "normalize_forecast",
    "normalize_resolution",
    "payload_digest",
    "read_ledger_bytes",
    "sha256_hex",
    "verify_bytes",
    "verify_ledger",
]
