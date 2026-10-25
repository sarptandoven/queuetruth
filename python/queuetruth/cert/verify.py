"""`queuetruth verify-cert`: re-check a `qt.certificate` against the files on disk and, with `--rerun`, against a
fresh execution of every run.

Checks (ids in report order; `fail` makes the verdict `fail`, `warn` does not):

- `schema`: the document is a valid `qt.certificate` with unique run labels, input ids and claim ids (nothing else
  runs when it is not).
- `integrity`: sha256 of the canonical body equals `integrity.body_sha256` and `certificate_id` is its prefix.
- `signature`: the Ed25519 signature verifies (`warn` for an unsigned certificate, `fail` with
  `--require-signature`).
- `key_pin`: with `--trust FINGERPRINT` / `--pubkey PEM`, the signature verifies and its key is one of them; without
  them a signed certificate gets `warn` (a valid signature by an unknown key proves only that the body was not
  changed after someone with that key signed it).
- `input:<id>` / `attachment:<id>`: every file re-hashed and its size compared (missing or changed = fail, extra
  files = warn), the identity recomputed and, for a dataset, its manifest, `dataset_id` and data time span
  re-derived.
- `outputs:<run>`: every output file re-hashed, raw and normalized (same rule).
- `binding:<run>`: the document's kind, schema and spec equal the certified ones, every input the spec names is the
  certified input at that path, and the run id recomputed from the spec and the current input identities equals
  the certified run id (and the one the document records). This binds the summary document to the inputs; only
  `--rerun` shows that the outputs follow from them.
- `claim:<id>`: the evidence is exactly the references of the claim text (run, file, pointer), every cited file is
  a certified output that passed its hash check, every cited value read again equals the certified value, and the
  rendered text follows from the values.
- `commitment:<n>`: the commitment file is unchanged and internally consistent, the revealed items open its leaves,
  their matches to certificate inputs recompute from the certified file hashes, its signature verifies, and
  `earliest_data_ts` / `precedes_data` recompute.
- `rerun:<run>` (`--rerun`): the run executed again from the certified spec in a directory `verify-cert` creates
  (child process with a safe import path, working directory = base) produces every certified output with an equal
  normalized digest (SPEC 7.2: JSON without `timings` and without the execution-only fields of the spec echo;
  every other file byte for byte; paths written under `--map` locations mapped back). An output that differs only
  within the SPEC 7.2 cross-platform tolerance (4 ulp, `queuetruth.cert.tolerance`) is `equivalent` (`warn`).
  `rerun_claim:<id>` re-reads the claim values from the re-executed outputs. Re-execution is skipped when the
  integrity, signature or key-pin check failed (`--force-rerun` runs it anyway).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from queuetruth.cert import keys
from queuetruth.cert.certify import (
    certificate_body,
    dataset_facts,
    earliest_data,
    inputs_by_sha,
    parse_claim,
    read_value,
    ref_text,
    render_claim,
)
from queuetruth.cert.commit import Commitment, leaf, load_doc, signature_check, structure_check
from queuetruth.cert.hashing import map_bytes, normalized_sha256, sha256_bytes, sha256_file, tree_files
from queuetruth.cert.models import (
    Certificate,
    CertInput,
    CertRun,
    CertVerification,
    Check,
    CheckCounts,
    RunVerification,
    VerifiedCertificate,
)
from queuetruth.cert.runs import (
    detect,
    executable_spec,
    map_path,
    map_spec,
    recompute_run_id,
    resolve_under,
    spec_refs,
)
from queuetruth.cert.tolerance import equivalent, json_close
from queuetruth.errors import QtError
from queuetruth.schemas.runid import canonical_json, spec_echo

MAX_DETAILS = 20
#: Default limit of one re-execution (`verify-cert --timeout`).
RERUN_TIMEOUT_S = 6 * 3600
#: Run kinds whose spec accepts the execution-only `threads` (`verify-cert --threads`).
THREADED_KINDS = ("replay", "zoo", "calibrate", "calibrate_live")
#: Checks whose failure means the certificate itself cannot be trusted (re-execution is skipped after one).
GATE_CHECKS = ("integrity", "signature", "key_pin")


def _cap(items: list[str]) -> list[str]:
    if len(items) <= MAX_DETAILS:
        return items
    return [*items[:MAX_DETAILS], f"... and {len(items) - MAX_DETAILS} more"]


class _Ctx:
    """Verification state."""

    def __init__(self, base: Path, mapping: list[tuple[str, str]]) -> None:
        self.base = base
        self.mapping = mapping
        self.checks: list[Check] = []
        self.identities: dict[str, str | None] = {}
        self.input_ok: dict[str, bool] = {}
        self.inputs: dict[str, CertInput] = {}
        #: run label -> {output path: normalized sha256} of every certified output that passed its hash check.
        self.verified_outputs: dict[str, dict[str, str]] = {}
        #: run label -> {output path: normalized sha256 recomputed from the file on disk} of every certified output
        #: whose raw sha256 matched (what a re-execution must reproduce, whatever the certificate states).
        self.on_disk: dict[str, dict[str, str]] = {}
        self.warnings: list[str] = []

    def add(self, id_: str, status: str, summary: str, details: list[str] | None = None) -> Check:
        ch = Check(id=id_, status=status, summary=summary, details=_cap(details or []))  # type: ignore[arg-type]
        self.checks.append(ch)
        return ch

    def path(self, given: str) -> Path:
        return resolve_under(self.base, map_path(given, self.mapping))

    def failed(self, *ids: str) -> bool:
        return any(c.id in ids and c.status == "fail" for c in self.checks)


def _compare_files(want: list[tuple[str, str]], got: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """(missing, changed, extra) between certified (path, sha256) pairs and current {path: sha256}."""
    want_map = dict(want)
    missing = [p for p, _ in want if p not in got]
    changed = [p for p, s in want if p in got and got[p] != s]
    extra = sorted(p for p in got if p not in want_map)
    return missing, changed, extra


def _sizes(want: list[tuple[str, int]], got: dict[str, int]) -> list[str]:
    return [f"size of {p}: certified {b}, now {got[p]}" for p, b in want if p in got and got[p] != b]


def _read_manifest(d: Path) -> dict[str, Any] | None:
    mp = d / "manifest.json"
    if not mp.is_file():
        return None
    try:
        doc = json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def check_input(ctx: _Ctx, inp: CertInput, prefix: str) -> None:
    """Re-hashes one input or attachment and re-derives what the certificate states about it."""
    cid = f"{prefix}:{inp.id}"
    p = ctx.path(inp.path)
    ctx.identities[inp.id] = None
    ctx.input_ok[inp.id] = False
    ctx.inputs[inp.id] = inp
    if inp.kind != "dataset" and (
        inp.dataset_id is not None or inp.dataset_consistent is not None or inp.data_ts_min is not None
    ):
        ctx.add(cid, "fail", f"{inp.path}: a {inp.kind} input carries dataset fields")
        return
    if inp.kind == "file":
        if not p.is_file():
            ctx.add(cid, "fail", f"{inp.path}: file not found (resolved to {p})")
            return
        sha = sha256_file(p)
        size = p.stat().st_size
        ctx.identities[inp.id] = sha
        details = []
        if sha != inp.identity or len(inp.files) != 1 or inp.files[0].sha256 != sha:
            details.append(f"certified {inp.identity}, now {sha}")
        if size != inp.bytes or (inp.files and inp.files[0].bytes != size):
            details.append(f"certified {inp.bytes} bytes, now {size}")
        if details:
            ctx.add(cid, "fail", f"{inp.path}: contents changed", details)
            return
        ctx.input_ok[inp.id] = True
        ctx.add(cid, "pass", f"{inp.path}: {inp.bytes} bytes, sha256 matches")
        return
    if not p.is_dir():
        ctx.add(cid, "fail", f"{inp.path}: directory not found (resolved to {p})")
        return
    try:
        tree = tree_files(p)
    except QtError as e:
        ctx.add(cid, "fail", f"{inp.path}: {e.message}")
        return
    got = {rel: s for rel, _, s in tree}
    sizes = {rel: b for rel, b, _ in tree}
    missing, changed, extra = _compare_files([(f.path, f.sha256) for f in inp.files], got)
    details = [f"missing: {x}" for x in missing] + [f"changed: {x}" for x in changed]
    details += _sizes([(f.path, f.bytes) for f in inp.files], sizes)
    if inp.bytes != sum(f.bytes for f in inp.files):
        details.append(f"bytes {inp.bytes} is not the sum of the certified file sizes")
    manifest = _read_manifest(p)
    is_dataset = manifest is not None and manifest.get("schema") == "qt.dataset_manifest"
    if is_dataset != (inp.kind == "dataset"):
        details.append(f"certified as a {inp.kind}, but it {'is' if is_dataset else 'is not'} a dataset")
    if inp.kind == "dataset":
        ok, problems, ts_min, ts_max = dataset_facts(got, manifest or {})
        identity = (manifest or {}).get("dataset_id")
        if not ok:
            details.extend(problems)
        if inp.dataset_id != identity:
            details.append(f"dataset_id {inp.dataset_id} is now {identity}")
        if inp.dataset_consistent is not ok:
            details.append(f"dataset_consistent {inp.dataset_consistent} does not recompute ({ok})")
        if (inp.data_ts_min, inp.data_ts_max) != (ts_min, ts_max):
            details.append(
                f"data span {inp.data_ts_min}..{inp.data_ts_max} does not re-derive from manifest.json "
                f"({ts_min}..{ts_max})"
            )
    else:
        from queuetruth.score._io import resolve_ref

        try:
            identity = resolve_ref({"path": str(p)})
        except QtError:
            identity = None
    ctx.identities[inp.id] = identity if isinstance(identity, str) else None
    if identity != inp.identity:
        details.append(f"identity {inp.identity} is now {identity}")
    if details:
        ctx.add(
            cid,
            "fail",
            f"{inp.path}: {len(missing)} missing, {len(changed)} changed of {len(inp.files)} files",
            details,
        )
        return
    ctx.input_ok[inp.id] = True
    if extra:
        ctx.add(
            cid,
            "warn",
            f"{inp.path}: {len(inp.files)} files match; {len(extra)} extra file(s) not certified",
            [f"extra: {x}" for x in extra],
        )
        return
    ctx.add(cid, "pass", f"{inp.path}: all {len(inp.files)} files match ({inp.bytes} bytes)")


def check_outputs(ctx: _Ctx, run: CertRun) -> bool:
    """Re-hashes the run directory: raw sha256, size and normalized sha256 of every certified output."""
    cid = f"outputs:{run.label}"
    ctx.verified_outputs[run.label] = {}
    ctx.on_disk[run.label] = {}
    p = ctx.path(run.path)
    if not p.is_dir():
        ctx.add(cid, "fail", f"{run.path}: run directory not found (resolved to {p})")
        return False
    try:
        tree = tree_files(p)
    except QtError as e:
        ctx.add(cid, "fail", f"{run.path}: {e.message}")
        return False
    got = {rel: s for rel, _, s in tree}
    sizes = {rel: b for rel, b, _ in tree}
    missing, changed, extra = _compare_files([(o.path, o.sha256) for o in run.outputs], got)
    details = [f"missing: {x}" for x in missing] + [f"changed: {x}" for x in changed]
    details += _sizes([(o.path, o.bytes) for o in run.outputs], sizes)
    bad = set(missing) | set(changed)
    for o in run.outputs:
        if o.path in bad:
            continue
        norm = normalized_sha256(p / o.path, got[o.path])
        ctx.on_disk[run.label][o.path] = norm
        if norm != o.normalized_sha256:
            details.append(f"normalized sha256 of {o.path} does not recompute: certified {o.normalized_sha256}")
            bad.add(o.path)
        else:
            ctx.verified_outputs[run.label][o.path] = norm
    if details:
        ctx.add(
            cid,
            "fail",
            f"{run.path}: {len(missing)} missing, {len(bad) - len(missing)} changed of {len(run.outputs)} outputs",
            details,
        )
        return False
    if extra:
        ctx.add(
            cid,
            "warn",
            f"{run.path}: {len(run.outputs)} outputs match; {len(extra)} extra file(s) not certified",
            [f"extra: {x}" for x in extra],
        )
        return True
    ctx.add(cid, "pass", f"{run.path}: all {len(run.outputs)} outputs match")
    return True


def check_binding(ctx: _Ctx, run: CertRun) -> bool:
    """Kind, schema and spec equality, input paths, and run-id recomputation from the current inputs."""
    cid = f"binding:{run.label}"
    details: list[str] = []
    try:
        rd = detect(ctx.path(run.path))
    except QtError as e:
        ctx.add(cid, "fail", f"{run.path}: {e.message}")
        return False
    if rd.kind != run.kind:
        details.append(f"the run is a {rd.kind} run, certified as {run.kind}")
    if rd.document != run.document or rd.schema != run.document_schema:
        details.append(f"document is now {rd.document} ({rd.schema})")
    if canonical_json(rd.spec) != canonical_json(run.spec):
        details.append(f"the spec echo in {rd.document} differs from the certified spec")
    if rd.recorded_run_id is not None and rd.recorded_run_id != run.run_id:
        details.append(f"{rd.document} records run_id {rd.recorded_run_id}, certified {run.run_id}")
    if (rd.recorded_run_id is not None) != (run.binding == "verified"):
        details.append(f"binding {run.binding} does not match the document")
    refs = spec_refs(run.spec)
    by_pointer = {ri.pointer: ri.input for ri in run.inputs}
    if len(by_pointer) != len(run.inputs) or set(by_pointer) != {ptr for ptr, _ in refs}:
        details.append("the certified inputs are not exactly the references of the spec")
    unresolved = []
    refmap: dict[str, str] = {}
    for pointer, ref in refs:
        inp = ctx.inputs.get(by_pointer.get(pointer, ""))
        if inp is not None and ref.get("path") != inp.path:
            details.append(f"spec{pointer} names {ref.get('path')!r}, its certified input is {inp.path!r}")
        ident = ctx.identities.get(by_pointer.get(pointer, ""))
        if ident is None:
            unresolved.append(pointer)
        else:
            refmap[canonical_json(ref)] = ident
    if unresolved:
        details.append(f"inputs not available: {', '.join(unresolved)}")
    else:
        rid = recompute_run_id(
            executable_spec(run.kind, run.spec), run.engine_version, lambda r: refmap[canonical_json(r)]
        )
        if rid != run.run_id:
            details.append(f"run_id recomputes to {rid}, certified {run.run_id}")
    if details:
        ctx.add(cid, "fail", f"run {run.label}: the inputs no longer bind to the certified run", details)
        return False
    how = "recorded and recomputed" if run.binding == "verified" else "recomputed (the document records none)"
    ctx.add(cid, "pass", f"run {run.label}: run_id {run.run_id} {how} from the current inputs")
    return True


def _evidence_problems(cert: Certificate, c: Any) -> list[str]:
    """Problems of a claim's evidence list against its text: each entry must be exactly the reference at its place
    in the text, naming a certified output of a certified run."""
    try:
        parts = parse_claim(c.text)
    except QtError as err:
        return [err.message]
    refs = [p for p in parts if not isinstance(p, str)]
    if len(refs) != len(c.evidence):
        return [f"the text cites {len(refs)} value(s), the evidence lists {len(c.evidence)}"]
    runs = {r.label: r for r in cert.runs}
    out: list[str] = []
    for (label, file, pointer), e in zip(refs, c.evidence, strict=True):
        run = runs.get(label)
        if run is None:
            out.append(f"{ref_text(label, file, pointer)}: no certified run {label!r}")
            continue
        want_file = file if file is not None else run.document
        if (e.ref, e.run, e.file, e.pointer) != (ref_text(label, file, pointer), label, want_file, pointer):
            out.append(f"evidence {e.ref} ({e.run}:{e.file}#{e.pointer}) is not the reference of the text")
        elif want_file not in {o.path for o in run.outputs}:
            out.append(f"{e.ref}: {want_file} is not a certified output of run {label!r}")
    if not out and render_claim(parts, [e.value for e in c.evidence]) != c.rendered:
        out.append("the rendered text does not follow from the text and the certified values")
    return out


def check_claims(
    ctx: _Ctx, cert: Certificate, roots: dict[str, Path], prefix: str = "claim", why_absent: str = ""
) -> None:
    """Re-reads every cited value under `roots[run]` (the run directory, or its re-executed copy). For `claim`, a
    cited file must also have passed its hash check; for `rerun_claim`, values are compared within the SPEC 7.2
    tolerance (a difference within it is a warning)."""
    for c in cert.claims:
        cid = f"{prefix}:{c.id}"
        details = _evidence_problems(cert, c)
        if details:
            ctx.add(cid, "fail", f"claim {c.id} does not hold: {c.rendered}", details)
            continue
        absent = sorted({e.run for e in c.evidence if e.run not in roots})
        if absent:
            why = why_absent or ("not selected (--only)" if prefix == "claim" else "not re-executed")
            ctx.add(cid, "skip", f"claim {c.id} cites run(s) {', '.join(absent)}: {why}")
            continue
        near: list[str] = []
        for e in c.evidence:
            if prefix == "claim" and e.file not in ctx.verified_outputs.get(e.run, {}):
                details.append(f"{e.ref}: {e.file} did not pass its hash check")
                continue
            try:
                v = read_value(roots[e.run], e.file, e.pointer, e.ref)
            except QtError as err:
                details.append(err.message)
                continue
            if canonical_json(v) == canonical_json(e.value):
                continue
            if prefix != "claim" and json_close(e.value, v):
                near.append(f"{e.ref}: certified {canonical_json(e.value)}, now {canonical_json(v)} (within 4 ulp)")
            else:
                details.append(f"{e.ref}: certified {canonical_json(e.value)}, now {canonical_json(v)}")
        if details:
            ctx.add(cid, "fail", f"claim {c.id} does not hold: {c.rendered}", details)
        elif near:
            ctx.add(cid, "warn", f"claim {c.id}: values equal within the SPEC 7.2 tolerance: {c.rendered}", near)
        else:
            ctx.add(cid, "pass", f"claim {c.id}: {len(c.evidence)} value(s) equal: {c.rendered}")


def check_commitments(ctx: _Ctx, cert: Certificate) -> None:
    """Re-verifies the linked commitments (their signatures are checked; their keys are not pinned, since the
    committer is usually not the certificate's issuer: the details name each key)."""
    by_sha = inputs_by_sha(cert.inputs + cert.attachments)
    for i, link in enumerate(cert.commitments, start=1):
        cid = f"commitment:{i}"
        p = ctx.path(link.path)
        details: list[str] = []
        if not p.is_file():
            ctx.add(cid, "fail", f"{link.path}: commitment file not found")
            continue
        if sha256_file(p) != link.sha256:
            details.append("the commitment file changed")
        try:
            c, raw = load_doc(p, Commitment)
        except QtError as e:
            ctx.add(cid, "fail", f"{link.path}: {e.message}")
            continue
        structure = structure_check(c)
        if structure.status == "fail":
            details.extend(structure.details)
        if c.root != link.root or c.committed_at != link.committed_at or c.label != link.label:
            details.append("root, label or committed_at differ from the certified commitment")
        fp_now = c.signature.key_fingerprint if c.signature is not None else None
        if fp_now != link.key_fingerprint:
            details.append(f"signing key {fp_now} differs from the certified {link.key_fingerprint}")
        for it in link.revealed:
            if not (0 <= it.index < len(c.leaves)) or leaf(it.nonce, it.name, it.sha256) != c.leaves[it.index]:
                details.append(f"item {it.index} ({it.name}) does not open its leaf")
            want = by_sha.get(it.sha256, [])
            if it.inputs != want:
                details.append(
                    f"item {it.index} ({it.name}): linked inputs {it.inputs} do not recompute from the certified "
                    f"file hashes ({want})"
                )
            for inp_id in want:
                if inp_id in ctx.input_ok and not ctx.input_ok[inp_id]:
                    details.append(f"item {it.index} ({it.name}): linked input {inp_id} did not verify")
        checks, fp, _ = signature_check(c, raw, None)
        details.extend(f"{ch.id}: {ch.summary}" for ch in checks if ch.status == "fail")
        earliest, precedes = earliest_data(link.revealed, cert.inputs + cert.attachments, cert.runs, c.committed_at)
        if (earliest, precedes) != (link.earliest_data_ts, link.precedes_data):
            details.append("earliest_data_ts / precedes_data do not recompute")
        if details:
            ctx.add(cid, "fail", f"commitment {i} ({link.path}) does not verify", details)
        else:
            when = "before" if link.precedes_data else "not before" if link.precedes_data is False else "unrelated to"
            ctx.add(
                cid,
                "pass",
                f"commitment {i}: {len(link.revealed)} revealed item(s) open; committed {when} the data",
                [f"signed by key {fp}" if fp else "unsigned commitment"],
            )


class Rerun:
    """Options of re-execution."""

    def __init__(self, threads: int | None = None, timeout_s: float = RERUN_TIMEOUT_S) -> None:
        self.threads = threads
        self.timeout_s = timeout_s


def _child_env() -> dict[str, str]:
    """The child's environment: the verifier's, with the working directory kept off the import path."""
    env = dict(os.environ)
    env["PYTHONSAFEPATH"] = "1"
    return env


def rerun_one(ctx: _Ctx, run: CertRun, wdir: Path, opts: Rerun) -> tuple[str, list[str], list[str], Path | None]:
    """Executes one run again into `wdir/<label>` (a directory that must not exist yet); returns (outcome,
    differing files, files equal only within tolerance, output dir)."""
    cid = f"rerun:{run.label}"
    out = wdir / run.label
    if out.resolve().parent != wdir or out.exists() or out.is_symlink():
        ctx.add(cid, "fail", f"run {run.label}: {out} is not a new directory directly under {wdir}")
        return "failed", [], [], None
    spec = spec_echo(map_spec(executable_spec(run.kind, run.spec), ctx.mapping))
    if opts.threads is not None and run.kind in THREADED_KINDS:
        spec["threads"] = opts.threads
    req = {"kind": run.kind, "spec": spec, "out": str(out)}
    try:
        p = subprocess.run(
            [sys.executable, "-P", "-m", "queuetruth.cert.rerun"],
            input=json.dumps(req),
            capture_output=True,
            text=True,
            cwd=ctx.base,
            env=_child_env(),
            timeout=opts.timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        ctx.add(cid, "fail", f"run {run.label}: re-execution timed out after {opts.timeout_s:g} s")
        return "failed", [], [], None
    if p.returncode != 0:
        msg = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else p.stderr.strip()[-500:]
        try:
            msg = json.loads(msg).get("message", msg)
        except (ValueError, AttributeError):
            pass
        ctx.add(cid, "fail", f"run {run.label}: re-execution failed: {msg}")
        return "failed", [], [], None
    if not out.is_dir():
        ctx.add(cid, "fail", f"run {run.label}: re-execution wrote no output directory")
        return "failed", [], [], None
    unmap = [(new, old) for old, new in ctx.mapping]
    tree = tree_files(out)
    files: dict[str, tuple[str, int]] = {}
    if unmap:  # files that name a mapped path, and the digests other outputs record of them
        for rel, _, s in tree:
            if not rel.lower().endswith(".json"):
                back = map_bytes((out / rel).read_bytes(), unmap)
                if (b := sha256_bytes(back)) != s:
                    files[s] = (b, len(back))
    got = {rel: normalized_sha256(out / rel, s, unmap, files) for rel, _, s in tree}
    # compare with the digests recomputed from the certified files when they passed their hash check
    on_disk = ctx.on_disk.get(run.label, {})
    want = [(o.path, on_disk.get(o.path, o.normalized_sha256)) for o in run.outputs]
    missing, changed, extra = _compare_files(want, got)
    run_dir = ctx.path(run.path)
    close = [f for f in changed if f in on_disk and equivalent(run_dir / f, out / f, unmap)]
    differing = missing + [f for f in changed if f not in close]
    if differing:
        ctx.add(
            cid,
            "fail",
            f"run {run.label}: {len(differing)} of {len(run.outputs)} outputs differ after re-execution",
            [f"not reproduced: {x}" for x in missing]
            + [f"differs: {x}" for x in changed if x not in close]
            + [f"within 4 ulp: {x}" for x in close],
        )
        return "differs", differing, close, out
    notes = [f"within 4 ulp (SPEC 7.2 cross-platform tolerance): {x}" for x in close]
    notes += [f"extra: {x}" for x in extra]
    if close or extra:
        what = (
            f"{len(run.outputs) - len(close)} outputs identical, {len(close)} equal within 4 ulp"
            if close
            else f"all {len(run.outputs)} outputs identical"
        )
        tail = f"; the re-execution also wrote {len(extra)} file(s) the certificate does not list" if extra else ""
        ctx.add(cid, "warn", f"run {run.label}: {what}{tail}", notes)
    else:
        ctx.add(cid, "pass", f"run {run.label}: all {len(run.outputs)} outputs identical")
    return ("equivalent" if close else "identical"), [], close, out


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys_ = [k for k, _ in pairs]
    dup = sorted(k for k, n in Counter(keys_).items() if n > 1)
    if dup:
        raise ValueError(f"duplicate object key(s) {dup}")
    return dict(pairs)


def _load(path: Path) -> dict[str, Any]:
    """The certificate as JSON; duplicate object keys are refused, so every JSON parser reads the same body."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    except OSError as e:
        raise QtError("QT-INPUT-001", f"{path}: {e.strerror or e}") from None
    except (UnicodeDecodeError, ValueError) as e:
        raise QtError("QT-INPUT-002", f"{path}: invalid JSON ({e})") from None
    if not isinstance(raw, dict) or raw.get("schema") != Certificate.SCHEMA:
        got = raw.get("schema") if isinstance(raw, dict) else type(raw).__name__
        raise QtError("QT-INPUT-002", f"{path}: expected a qt.certificate document, got {got!r}")
    return raw


def _duplicates(cert: Certificate) -> list[str]:
    out = []
    for what, ids in (
        ("run label", [r.label for r in cert.runs]),
        ("input id", [x.id for x in cert.inputs + cert.attachments]),
        ("claim id", [c.id for c in cert.claims]),
    ):
        out += [f"{what} {k!r} appears {n} times" for k, n in Counter(ids).items() if n > 1]
    return out


def _work_dir(ctx: _Ctx, cert: Certificate, work: Path) -> Path:
    """`--work`: a new or empty directory that is not, inside or above any certified path."""
    wdir = work.resolve()
    if wdir.exists() and (not wdir.is_dir() or any(wdir.iterdir())):
        raise QtError(
            "QT-USAGE-001",
            f"--work {work}: must be a new or empty directory",
            "verify-cert writes each re-executed run into <work>/<label> and never replaces existing files",
        )
    certified = [r.path for r in cert.runs] + [x.path for x in cert.inputs + cert.attachments]
    certified += [c.path for c in cert.commitments]
    for given in certified:
        q = ctx.path(given).resolve()
        if wdir == q or wdir.is_relative_to(q) or q.is_relative_to(wdir):
            raise QtError("QT-USAGE-001", f"--work {work}: overlaps the certified path {given}", "use another path")
    return wdir


def _environment_warnings(ctx: _Ctx, cert: Certificate) -> None:
    from queuetruth.cert.util import environment

    now = environment()
    then = cert.environment
    if then.queuetruth != now["queuetruth"]:
        ctx.warnings.append(
            f"certified with queuetruth {then.queuetruth}, re-executed with {now['queuetruth']}: determinism is "
            "promised within one version only (SPEC 7.2)"
        )
    if (then.platform, then.machine) != (now["platform"], now["machine"]):
        ctx.warnings.append(
            f"certified on {then.platform} {then.machine}, re-executed on {now['platform']} {now['machine']}: values "
            "computed with powf, ln or exp may differ by up to 4 ulp across platforms (SPEC 7.2); such outputs are "
            "reported as equal within tolerance, other differences fail"
        )
    pk = [
        f"{name} {then.packages.get(name, 'absent')} -> {now['packages'].get(name, 'absent')}"
        for name in sorted(set(then.packages) | set(now["packages"]))
        if then.packages.get(name) != now["packages"].get(name)
    ]
    if pk:
        ctx.warnings.append(
            "package versions differ from the certifying environment ("
            + ", ".join(pk)
            + "): Parquet files written by pyarrow carry its version, and model pickles depend on scikit-learn and "
            "numpy, so such outputs may not be byte-identical"
        )


def verify_certificate(
    cert_path: Path,
    *,
    rerun: bool = False,
    only: list[str] = (),  # type: ignore[assignment]
    base: Path | None = None,
    mapping: list[tuple[str, str]] = (),  # type: ignore[assignment]
    trust: list[str] = (),  # type: ignore[assignment]
    pubkeys: list[Path] = (),  # type: ignore[assignment]
    require_signature: bool = False,
    work: Path | None = None,
    force_rerun: bool = False,
    threads: int | None = None,
    timeout_s: float = RERUN_TIMEOUT_S,
) -> CertVerification:
    """Runs every check; never raises for a failed check (only for an unreadable certificate or bad usage)."""
    raw = _load(cert_path)
    base = (base or Path.cwd()).resolve()
    ctx = _Ctx(base, list(mapping))
    trusted = {keys.normalize_fingerprint(t) for t in trust} | {keys.fingerprint(keys.load_public(p)) for p in pubkeys}
    try:
        cert = Certificate.model_validate(raw)
    except ValidationError as e:
        errs = [f"{'/'.join(str(x) for x in er['loc'])}: {er['msg']}" for er in e.errors()]
        ctx.add("schema", "fail", "not a valid qt.certificate document", errs)
        return _finish(ctx, cert_path, None, raw, rerun, [], False)
    dups = _duplicates(cert)
    if dups:
        ctx.add("schema", "fail", "not a valid qt.certificate document: identifiers are not unique", dups)
        return _finish(ctx, cert_path, None, raw, rerun, [], False)
    ctx.add("schema", "pass", "valid qt.certificate document")
    labels = [r.label for r in cert.runs]
    unknown = sorted(set(only) - set(labels))
    if unknown:
        raise QtError("QT-USAGE-001", f"--only {', '.join(unknown)}: no such run", f"runs: {', '.join(labels)}")
    wdir = _work_dir(ctx, cert, work) if rerun and work is not None else None
    digest = sha256_bytes(canonical_json(certificate_body(raw)).encode("utf-8"))
    if digest != cert.integrity.body_sha256 or cert.certificate_id != digest[:16]:
        ctx.add(
            "integrity",
            "fail",
            "the certificate body was changed after it was sealed",
            [f"sealed {cert.integrity.body_sha256}", f"now {digest}"],
        )
    else:
        ctx.add("integrity", "pass", f"body sha256 {digest}")
    sig = cert.integrity.signature
    sig_ok = False
    if sig is None:
        ctx.add(
            "signature",
            "fail" if require_signature else "warn",
            "unsigned: the digest detects accidental change only; whoever edits the certificate can recompute it",
        )
    else:
        sig_ok, why = keys.verify(sig, keys.CERT_DOMAIN, certificate_body(raw))
        ctx.add("signature", "pass" if sig_ok else "fail", why, [f"key {sig.key_fingerprint}"])
    key_trusted = False
    if trusted:
        listed = sig is not None and sig.key_fingerprint in trusted
        key_trusted = listed and sig_ok
        if key_trusted:
            summary = "signed by a trusted key"
        elif listed:
            summary = "the signature names a trusted key but does not verify"
        else:
            summary = "not signed by any of the trusted keys"
        ctx.add("key_pin", "pass" if key_trusted else "fail", summary, [f"trusted: {t}" for t in sorted(trusted)])
    elif sig is not None:
        ctx.add(
            "key_pin", "warn", "signing key not pinned: pass --trust FINGERPRINT or --pubkey PEM to check who signed"
        )
    selected = [r for r in cert.runs if not only or r.label in only]
    needed = {ri.input for r in selected for ri in r.inputs}
    for inp in cert.inputs:
        ctx.inputs[inp.id] = inp
        if not only or inp.id in needed:
            check_input(ctx, inp, "input")
    for att in cert.attachments:
        check_input(ctx, att, "attachment")
    states: dict[str, dict[str, Any]] = {}
    for run in selected:
        states[run.label] = {"outputs_ok": check_outputs(ctx, run), "binding_ok": check_binding(ctx, run)}
    check_claims(ctx, cert, {r.label: ctx.path(r.path) for r in selected})
    check_commitments(ctx, cert)
    if rerun:
        _environment_warnings(ctx, cert)
        if ctx.failed(*GATE_CHECKS) and not force_rerun:
            failed = ", ".join(c for c in GATE_CHECKS if ctx.failed(c))
            for run in selected:
                ctx.add(
                    f"rerun:{run.label}",
                    "skip",
                    f"run {run.label}: not re-executed because {failed} failed (--force-rerun runs it anyway)",
                )
                states[run.label]["rerun"] = "skipped"
            check_claims(ctx, cert, {}, prefix="rerun_claim", why_absent=f"not re-executed ({failed} failed)")
        else:
            _rerun_all(ctx, cert, selected, states, wdir, Rerun(threads, timeout_s))
    runs = [
        RunVerification(
            label=r.label,
            kind=r.kind,
            run_id=r.run_id,
            inputs_ok=all(ctx.input_ok.get(ri.input, False) for ri in r.inputs),
            outputs_ok=bool(states[r.label]["outputs_ok"]),
            binding_ok=bool(states[r.label]["binding_ok"]),
            rerun=states[r.label].get("rerun", "not_run"),
            differing=states[r.label].get("differing", []),
            within_tolerance=states[r.label].get("within_tolerance", []),
        )
        for r in selected
    ]
    return _finish(ctx, cert_path, cert, raw, rerun, runs, key_trusted if trusted else None)


def _rerun_all(
    ctx: _Ctx,
    cert: Certificate,
    selected: list[CertRun],
    states: dict[str, dict[str, Any]],
    wdir: Path | None,
    opts: Rerun,
) -> None:
    """Re-executes the selected runs into `wdir` (created here; a removed temporary directory when None)."""
    tmp = None
    if wdir is None:
        tmp = Path(tempfile.mkdtemp(prefix="qt-verify-")).resolve()
        wdir = tmp
    else:
        wdir.mkdir(parents=True, exist_ok=True)
    try:
        roots: dict[str, Path] = {}
        for run in selected:
            outcome, differing, close, out = rerun_one(ctx, run, wdir, opts)
            states[run.label].update(rerun=outcome, differing=differing, within_tolerance=close)
            if out is not None:
                roots[run.label] = out
        check_claims(ctx, cert, roots, prefix="rerun_claim")
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def _finish(
    ctx: _Ctx,
    path: Path,
    cert: Certificate | None,
    raw: dict[str, Any],
    rerun: bool,
    runs: list[RunVerification],
    key_trusted: bool | None,
) -> CertVerification:
    counts = {s: sum(c.status == s for c in ctx.checks) for s in ("pass", "warn", "fail", "skip")}
    sig = cert.integrity.signature if cert is not None else None
    ident = raw.get("integrity") if isinstance(raw.get("integrity"), dict) else {}
    body_sha = ident.get("body_sha256") if isinstance(ident.get("body_sha256"), str) else None
    cid = raw.get("certificate_id")
    return CertVerification(
        certificate=VerifiedCertificate(
            path=str(path),
            certificate_id=cert.certificate_id if cert is not None else None,
            body_sha256=body_sha if cert is not None else None,
            issued_at=cert.issued_at if cert is not None else None,
            signed=sig is not None,
            key_fingerprint=sig.key_fingerprint if sig is not None else None,
            key_trusted=key_trusted if cert is not None else None,
        ),
        rerun=rerun,
        status="fail" if counts["fail"] else "pass",
        counts=CheckCounts.model_validate(counts),
        checks=ctx.checks,
        runs=runs,
        warnings=ctx.warnings + ([f"certificate_id {cid!r} could not be validated"] if cert is None else []),
    )
