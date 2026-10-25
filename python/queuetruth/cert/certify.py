"""`queuetruth certify`: build a `qt.certificate` from run directories, claims, attachments and commitments.

What `certify` checks before it writes anything:

- every run directory holds a supported summary document with a spec echo (`queuetruth.cert.runs`);
- every input its spec names exists under the base directory (relative paths resolve against it: pass the directory
  the commands ran in), is hashed file by file, and for a dataset its `manifest.json` partition hashes match the
  files and re-derive its `dataset_id`;
- the run id recomputed from the spec echo, the engine version and those input identities (SPEC 7.3) equals the run
  id the document records: the inputs on disk are the inputs the run used;
- every claim reference `{RUN#/pointer}` or `{RUN:FILE#/pointer}` names an output JSON file of a certified run and a
  value that exists there (never under `timings`, which is wall-clock and excluded from determinism);
- every commitment opening opens its commitment.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from queuetruth.cert import keys
from queuetruth.cert.commit import (
    Commitment,
    CommitmentOpening,
    load_doc,
    open_items,
    signature_check,
    structure_check,
)
from queuetruth.cert.hashing import (
    canonical_value_text,
    json_pointer,
    normalized_sha256,
    pointer_tokens,
    sha256_bytes,
    sha256_file,
    tree_files,
)
from queuetruth.cert.models import (
    LABEL_PATTERN,
    Certificate,
    CertInput,
    CertRun,
    Claim,
    CommitmentLink,
    Environment,
    Evidence,
    HashedFile,
    Integrity,
    OutputFile,
    Reproduce,
    RevealedItem,
    RunInput,
)
from queuetruth.cert.runs import (
    API_FUNCTIONS,
    RunDoc,
    detect,
    executable_spec,
    python_call,
    recompute_run_id,
    resolve_under,
    spec_refs,
)
from queuetruth.cert.util import environment, now_ns
from queuetruth.errors import QtError
from queuetruth.schemas.runid import canonical_json

LABEL_RE = re.compile(LABEL_PATTERN)
#: `{RUN#/pointer}` or `{RUN:FILE#/pointer}` inside claim text; `{{` and `}}` are literal braces.
REF_RE = re.compile(r"([A-Za-z0-9_.\-]{1,64})(?::([^#{}]+))?#(/[^{}]*)")
#: Fields a certificate body leaves out (the integrity part).
INTEGRITY_FIELDS = ("certificate_id", "integrity")


def certificate_body(doc: dict[str, Any]) -> dict[str, Any]:
    """The part of a certificate that `integrity` covers."""
    return {k: v for k, v in doc.items() if k not in INTEGRITY_FIELDS}


def dataset_id(shas: list[str], source_format: str) -> str:
    """SPEC 3.10 `dataset_id`: sha256 of the sorted partition file hashes, newline-joined, then a newline and the
    source format."""
    return sha256_bytes(("\n".join(sorted(shas)) + "\n" + source_format).encode())


def _rel(path: Path, base: Path) -> str:
    """POSIX path relative to `base` when inside it, else absolute."""
    try:
        return path.resolve().relative_to(base.resolve()).as_posix() or "."
    except ValueError:
        return path.resolve().as_posix()


# ---- inputs -------------------------------------------------------------------------------------------------------


def _manifest(files: dict[str, str], root: Path) -> dict[str, Any] | None:
    if "manifest.json" not in files:
        return None
    try:
        doc = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def dataset_facts(files: dict[str, str], manifest: dict[str, Any]) -> tuple[bool, list[str], int | None, int | None]:
    """(consistent, problems, ts_min, ts_max) of a dataset manifest against the hashed files {relpath: sha256}."""
    problems: list[str] = []
    shas: list[str] = []
    ts_min: int | None = None
    ts_max: int | None = None
    for part in manifest.get("partitions") or []:
        for t, pick in (("ts_min", min), ("ts_max", max)):
            v = part.get(t)
            if isinstance(v, int) and not isinstance(v, bool):
                if t == "ts_min":
                    ts_min = v if ts_min is None else pick(ts_min, v)
                else:
                    ts_max = v if ts_max is None else pick(ts_max, v)
        for f in part.get("files") or []:
            want = str(f.get("sha256") or "")
            shas.append(want)
            got = files.get(str(f.get("path") or ""))
            if got is None:
                problems.append(f"{f.get('path')}: listed in manifest.json but missing")
            elif got != want:
                problems.append(f"{f.get('path')}: sha256 differs from manifest.json")
    recomputed = dataset_id(shas, str(manifest.get("source_format") or ""))
    if recomputed != manifest.get("dataset_id"):
        problems.append(f"dataset_id {manifest.get('dataset_id')} does not re-derive (got {recomputed})")
    return not problems, problems, ts_min, ts_max


def hash_input(input_id: str, given: str, resolved: Path) -> tuple[CertInput, list[str]]:
    """Hashes one input (file, directory or dataset); returns it and dataset consistency problems."""
    if resolved.is_symlink() and not resolved.exists():
        raise QtError("QT-INPUT-001", f"{given}: dangling symbolic link")
    if resolved.is_file():
        sha = sha256_file(resolved)
        size = resolved.stat().st_size
        return (
            CertInput(
                id=input_id,
                path=given,
                kind="file",
                identity=sha,
                bytes=size,
                files=[HashedFile(path=resolved.name, bytes=size, sha256=sha)],
            ),
            [],
        )
    if not resolved.is_dir():
        raise QtError("QT-INPUT-001", f"{given}: not found (resolved to {resolved})")
    tree = tree_files(resolved)
    by_path = {p: s for p, _, s in tree}
    files = [HashedFile(path=p, bytes=b, sha256=s) for p, b, s in tree]
    total = sum(b for _, b, _ in tree)
    manifest = _manifest(by_path, resolved)
    if manifest is not None and manifest.get("schema") == "qt.dataset_manifest":
        ok, problems, lo, hi = dataset_facts(by_path, manifest)
        did = manifest.get("dataset_id")
        did = did if isinstance(did, str) and re.fullmatch(r"[0-9a-f]{64}", did) else None
        if did is None:
            raise QtError("QT-INPUT-002", f"{given}/manifest.json: no valid dataset_id")
        return (
            CertInput(
                id=input_id,
                path=given,
                kind="dataset",
                identity=did,
                bytes=total,
                files=files,
                dataset_id=did,
                dataset_consistent=ok,
                data_ts_min=lo,
                data_ts_max=hi,
            ),
            problems,
        )
    # SPEC 7.3 identity of a directory reference, as `queuetruth.score._io.resolve_ref` computes it: the sha256 of
    # its manifest.json (live logs), else a digest over its sorted files
    if "manifest.json" in by_path and not (manifest is not None and isinstance(manifest.get("dataset_id"), str)):
        identity = by_path["manifest.json"]
    else:
        from queuetruth.score._io import resolve_ref

        identity = resolve_ref({"path": str(resolved)})
    return CertInput(id=input_id, path=given, kind="directory", identity=identity, bytes=total, files=files), []


# ---- claims -------------------------------------------------------------------------------------------------------


def parse_claim(text: str) -> list[tuple[str, str | None, str] | str]:
    """Splits claim text into literal strings and (run, file, pointer) references; `QT-USAGE-001` on a brace that
    is neither a reference nor doubled."""
    parts: list[tuple[str, str | None, str] | str] = []
    buf: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "{" and text.startswith("{{", i):
            buf.append("{")
            i += 2
            continue
        if ch == "}" and text.startswith("}}", i):
            buf.append("}")
            i += 2
            continue
        if ch == "{":
            end = text.find("}", i)
            m = REF_RE.fullmatch(text[i + 1 : end]) if end > i else None
            if m is None:
                frag = text[i : end + 1] if end > i else text[i:]
                raise QtError(
                    "QT-USAGE-001",
                    f"claim {text!r}: {frag!r} is not a result reference",
                    "write {RUN#/json/pointer} or {RUN:FILE#/json/pointer}; {{ and }} are literal braces",
                )
            if buf:
                parts.append("".join(buf))
                buf = []
            parts.append((m.group(1), m.group(2), m.group(3)))
            i = end + 1
            continue
        if ch == "}":
            raise QtError("QT-USAGE-001", f"claim {text!r}: unmatched '}}'", "write }} for a literal brace")
        buf.append(ch)
        i += 1
    if buf:
        parts.append("".join(buf))
    return parts


def ref_text(run: str, file: str | None, pointer: str) -> str:
    """The canonical spelling of a reference."""
    return f"{run}:{file}#{pointer}" if file else f"{run}#{pointer}"


def render_claim(parts: list[tuple[str, str | None, str] | str], values: list[Any]) -> str:
    """Claim text with each reference replaced by its value."""
    out: list[str] = []
    it = iter(values)
    for p in parts:
        out.append(p if isinstance(p, str) else canonical_value_text(next(it)))
    return "".join(out)


def read_value(run_dir: Path, file: str, pointer: str, what: str) -> Any:
    """The value at `pointer` in the JSON file `run_dir/file`; `QT-USAGE-001` when the reference is unusable."""
    if any(tok == "timings" for tok in pointer_tokens(pointer)):
        raise QtError("QT-USAGE-001", f"{what}: values under 'timings' are wall-clock and cannot back a claim")
    p = run_dir / file
    if not p.is_file():
        raise QtError("QT-USAGE-001", f"{what}: {file} is not a file of the run")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise QtError("QT-USAGE-001", f"{what}: {file} is not a JSON file ({e})") from None
    try:
        return json_pointer(doc, pointer)
    except KeyError as e:
        raise QtError("QT-USAGE-001", f"{what}: no value at {pointer} in {file} (token {e.args[0]!r})") from None


# ---- runs ---------------------------------------------------------------------------------------------------------


@dataclass
class RunArg:
    """A run directory named on the command line (`DIR` or `LABEL=DIR`)."""

    label: str
    path: str


def parse_run_args(args: list[str]) -> list[RunArg]:
    """`DIR` (label = directory name) or `LABEL=DIR`; labels unique, 1 to 64 of `[A-Za-z0-9_.-]`, not starting
    with a dot."""
    if not args:
        raise QtError("QT-USAGE-001", "certify: give at least one run directory")
    out: list[RunArg] = []
    for a in args:
        label, sep, path = a.partition("=")
        if not sep or not LABEL_RE.fullmatch(label):
            label, path = Path(a.rstrip("/")).name, a
        if not LABEL_RE.fullmatch(label):
            raise QtError(
                "QT-USAGE-001",
                f"{a}: run label {label!r} must be 1 to 64 of [A-Za-z0-9_.-], not starting with a dot",
                "use LABEL=DIR",
            )
        out.append(RunArg(label, path))
    seen: dict[str, str] = {}
    for r in out:
        if r.label in seen:
            raise QtError(
                "QT-USAGE-001",
                f"run label {r.label!r} names both {seen[r.label]} and {r.path}",
                "name them apart with LABEL=DIR",
            )
        seen[r.label] = r.path
    return out


@dataclass
class _Inputs:
    """Inputs of a certificate, deduplicated by resolved path."""

    base: Path
    items: list[CertInput] = field(default_factory=list)
    by_path: dict[Path, CertInput] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def add(self, given: str, prefix: str = "in") -> CertInput:
        resolved = resolve_under(self.base, given)
        key = resolved.resolve()
        if key in self.by_path:
            return self.by_path[key]
        n = sum(1 for x in self.items if x.id.startswith(prefix)) + 1
        inp, problems = hash_input(f"{prefix}{n}", given, resolved)
        self.problems.extend(f"{given}: {p}" for p in problems)
        self.items.append(inp)
        self.by_path[key] = inp
        return inp


def certify_run(arg: RunArg, base: Path, inputs: _Inputs, out_files: list[Path]) -> tuple[CertRun, RunDoc]:
    """Detects, binds and hashes one run."""
    run_dir = resolve_under(base, arg.path)
    rd = detect(run_dir)
    for f in out_files:
        try:
            f.resolve().relative_to(run_dir.resolve())
        except ValueError:
            continue
        raise QtError("QT-USAGE-001", f"{f}: inside the certified run directory {arg.path}", "write it elsewhere")
    run_inputs: list[RunInput] = []
    identities: dict[str, str] = {}
    for pointer, ref in spec_refs(rd.spec):
        path = ref.get("path")
        if not isinstance(path, str):
            raise QtError(
                "QT-INPUT-002",
                f"{arg.path}: spec{pointer} references a service object ({canonical_json(ref)})",
                "certify runs made on local files; service jobs are certified by their own operator",
            )
        try:
            inp = inputs.add(path)
        except QtError as e:
            if e.code == "QT-INPUT-001":
                e.hint = (
                    e.hint or f"relative spec paths resolve against --base ({base}); pass the directory the run ran in"
                )
            raise
        run_inputs.append(RunInput(pointer=pointer, input=inp.id))
        identities[canonical_json(ref)] = inp.identity
    from queuetruth.schemas.arrow import ENGINE_VERSION

    engine = rd.engine_version or ENGINE_VERSION
    rid = recompute_run_id(executable_spec(rd.kind, rd.spec), engine, lambda r: identities[canonical_json(r)])
    if rd.recorded_run_id is not None and rd.recorded_run_id != rid:
        raise QtError(
            "QT-INPUT-002",
            f"{arg.path}: the inputs under {base} do not reproduce run_id {rd.recorded_run_id} (got {rid})",
            "an input changed after the run, the run was launched from another directory (pass --base), or it was "
            f"made by another engine version (this is {ENGINE_VERSION})",
            details={"run": arg.label, "recorded": rd.recorded_run_id, "recomputed": rid},
        )
    outputs = [
        OutputFile(path=p, bytes=b, sha256=s, normalized_sha256=normalized_sha256(run_dir / p, s))
        for p, b, s in tree_files(run_dir)
    ]
    run = CertRun(
        label=arg.label,
        kind=rd.kind,  # type: ignore[arg-type]
        path=_rel(run_dir, base) if Path(arg.path).is_absolute() else Path(arg.path).as_posix(),
        document=rd.document,
        document_schema=rd.schema,
        run_id=rid,
        binding="verified" if rd.recorded_run_id is not None else "recomputed",
        engine_version=engine,
        spec=rd.spec,
        inputs=run_inputs,
        outputs=outputs,
        reproduce=Reproduce(
            api=f"queuetruth.api.{API_FUNCTIONS[rd.kind]}",
            python=python_call(rd.kind, rd.spec, f"rerun/{arg.label}"),
            verify=f"queuetruth verify-cert CERT --rerun --only {arg.label}",
        ),
    )
    return run, rd


def build_claims(texts: list[str], runs: dict[str, tuple[CertRun, Path]]) -> tuple[list[Claim], list[str]]:
    """Claims with their evidence; warnings for claims that cite no value."""
    claims: list[Claim] = []
    warnings: list[str] = []
    for i, text in enumerate(texts, start=1):
        cid = f"c{i}"
        if not text.strip():
            raise QtError("QT-USAGE-001", f"claim {cid} is empty")
        parts = parse_claim(text)
        evidence: list[Evidence] = []
        for p in parts:
            if isinstance(p, str):
                continue
            label, file, pointer = p
            if label not in runs:
                raise QtError(
                    "QT-USAGE-001",
                    f"claim {cid}: {ref_text(label, file, pointer)} names run {label!r}",
                    f"certified runs: {', '.join(sorted(runs)) or 'none'}",
                )
            run, run_dir = runs[label]
            fname = file if file is not None else run.document
            if fname not in {o.path for o in run.outputs}:
                raise QtError("QT-USAGE-001", f"claim {cid}: {fname} is not an output file of run {label!r}")
            value = read_value(run_dir, fname, pointer, f"claim {cid}")
            evidence.append(
                Evidence(ref=ref_text(label, file, pointer), run=label, file=fname, pointer=pointer, value=value)
            )
        if not evidence:
            warnings.append(f"claim {cid} cites no result value; it is recorded as text only")
        rendered = render_claim(parts, [e.value for e in evidence])
        claims.append(Claim(id=cid, text=text, rendered=rendered, evidence=evidence))
    return claims, warnings


def inputs_by_sha(inputs: list[CertInput]) -> dict[str, list[str]]:
    """{file sha256: ids of the inputs (in order) that hold a file with that content}: how revealed commitment items
    are matched to certificate inputs."""
    by_sha: dict[str, list[str]] = {}
    for inp in inputs:
        for f in inp.files:
            ids = by_sha.setdefault(f.sha256, [])
            if inp.id not in ids:
                ids.append(inp.id)
    return by_sha


def link_commitment(
    c_path: Path,
    o_path: Path,
    base: Path,
    inputs: list[CertInput],
    runs: list[CertRun],
    trusted: set[str] | None = None,
) -> CommitmentLink:
    """Checks an opening against its commitment and matches the revealed items to certificate inputs by sha256."""
    c, raw = load_doc(c_path, Commitment)
    structure = structure_check(c)
    if structure.status == "fail":
        raise QtError("QT-INPUT-002", f"{c_path}: {structure.summary}: {'; '.join(structure.details)}")
    opening, _ = load_doc(o_path, CommitmentOpening)
    if not opening.items:
        raise QtError("QT-INPUT-002", f"{o_path}: the opening opens no item")
    for it, ok in open_items(c, opening):
        if not ok:
            raise QtError("QT-INPUT-002", f"{o_path}: item {it.index} ({it.name}) does not open its leaf")
    checks, fp, _ = signature_check(c, raw, trusted)
    for ch in checks:
        if ch.status == "fail":
            raise QtError("QT-INPUT-002", f"{c_path}: {ch.id}: {ch.summary}")
    by_sha = inputs_by_sha(inputs)
    revealed = [
        RevealedItem(index=it.index, name=it.name, sha256=it.sha256, nonce=it.nonce, inputs=by_sha.get(it.sha256, []))
        for it in opening.items
    ]
    earliest, precedes = earliest_data(revealed, inputs, runs, c.committed_at)
    return CommitmentLink(
        path=_rel(c_path, base),
        sha256=sha256_file(c_path),
        root=c.root,
        label=c.label,
        committed_at=c.committed_at,
        key_fingerprint=fp,
        revealed=revealed,
        earliest_data_ts=earliest,
        precedes_data=precedes,
    )


def earliest_data(
    revealed: list[RevealedItem], inputs: list[CertInput], runs: list[CertRun], committed_at: int
) -> tuple[int | None, bool | None]:
    """Earliest `ts_event` of the datasets used by the runs that use a revealed item, and whether the commitment
    time precedes it."""
    linked = {i for it in revealed for i in it.inputs}
    by_id = {x.id: x for x in inputs}
    ts: list[int] = []
    for run in runs:
        ids = {ri.input for ri in run.inputs}
        if not ids & linked:
            continue
        for i in ids:
            x = by_id.get(i)
            if x is not None and x.kind == "dataset" and x.data_ts_min is not None:
                ts.append(x.data_ts_min)
    if not ts:
        return None, None
    lo = min(ts)
    return lo, committed_at < lo


def seal(body: dict[str, Any], key: keys.SigningKey | None) -> dict[str, Any]:
    """The full certificate document: body plus `certificate_id` and `integrity` (signed with `key` when given)."""
    digest = sha256_bytes(canonical_json(body).encode("utf-8"))
    sig = key.sign(keys.CERT_DOMAIN, body) if key is not None else None
    integrity = Integrity(body_sha256=digest, signature=sig).model_dump(mode="json")
    doc = {"schema": body["schema"], "schema_version": body["schema_version"], "certificate_id": digest[:16]}
    doc.update({k: v for k, v in body.items() if k not in ("schema", "schema_version")})
    doc["integrity"] = integrity
    return doc


def certify(
    run_args: list[str],
    *,
    claims: list[str] = (),  # type: ignore[assignment]
    base: Path | None = None,
    title: str | None = None,
    issuer: str | None = None,
    attach: list[str] = (),  # type: ignore[assignment]
    commitments: list[tuple[Path, Path]] = (),  # type: ignore[assignment]
    key: keys.SigningKey | None = None,
    out_files: list[Path] = (),  # type: ignore[assignment]
    cert_name: str = "cert.json",
) -> Certificate:
    """Builds (and validates) a certificate; `out_files` are refused inside run directories."""
    base = (base or Path.cwd()).resolve()
    if not base.is_dir():
        raise QtError("QT-INPUT-001", f"--base {base}: directory not found")
    inputs = _Inputs(base)
    runs: list[CertRun] = []
    run_dirs: dict[str, tuple[CertRun, Path]] = {}
    for arg in parse_run_args(list(run_args)):
        run, _ = certify_run(arg, base, inputs, list(out_files))
        runs.append(run)
        run_dirs[run.label] = (run, resolve_under(base, arg.path))
    att = _Inputs(base)
    for a in attach:
        att.add(a, prefix="att")
    certified: list[tuple[Path, str]] = [(k, f"input {v.path}") for k, v in inputs.by_path.items()]
    certified += [(k, f"attachment {v.path}") for k, v in att.by_path.items()]
    for c, o in commitments:
        certified += [(Path(c).resolve(), f"commitment {c}"), (Path(o).resolve(), f"opening {o}")]
    for f in out_files:
        fr = f.resolve()
        for where, what in certified:
            if fr == where or fr.is_relative_to(where):
                raise QtError("QT-USAGE-001", f"{f}: would overwrite the certified {what}", "write it elsewhere")
    if inputs.problems:
        raise QtError(
            "QT-INPUT-002",
            f"dataset inputs are inconsistent with their manifests: {'; '.join(inputs.problems[:5])}",
            "a dataset file changed after ingest; re-ingest it or restore the original files",
            details={"problems": inputs.problems},
        )
    claim_docs, warnings = build_claims(list(claims), run_dirs)
    if att.problems:
        warnings.extend(f"attachment {p}" for p in att.problems)
    links = [link_commitment(c, o, base, inputs.items + att.items, runs) for c, o in commitments]
    for i, link in enumerate(links, start=1):
        if not any(it.inputs for it in link.revealed):
            warnings.append(f"commitment {i} ({link.path}): no revealed item matches a certified input or attachment")
        warnings.append(
            f"commitment {i} ({link.path}): committed_at is asserted by its committer"
            + (f" (key {link.key_fingerprint[:16]})" if link.key_fingerprint else " (unsigned)")
            + "; it is evidence of time only if the root was anchored elsewhere before the data existed"
        )
    for r in runs:
        if r.binding == "recomputed":
            warnings.append(
                f"run {r.label}: its {r.document} records no run_id, so the inputs are bound to the outputs only by "
                "re-execution (verify-cert --rerun)"
            )
    env = Environment.model_validate(environment())
    body: dict[str, Any] = {
        "schema": Certificate.SCHEMA,
        "schema_version": 1,
        "title": title,
        "issuer": issuer,
        "issued_at": now_ns(),
        "environment": env.model_dump(mode="json"),
        "runs": [r.model_dump(mode="json") for r in runs],
        "inputs": [x.model_dump(mode="json") for x in inputs.items],
        "claims": [c.model_dump(mode="json") for c in claim_docs],
        "attachments": [x.model_dump(mode="json") for x in att.items],
        "commitments": [x.model_dump(mode="json") for x in links],
        "reproduce": [
            "# from the directory certify ran in (relative paths resolve against it), or pass --base DIR",
            f'pip install "queuetruth=={env.queuetruth}"',
            f"queuetruth verify-cert {cert_name}" + (" --trust " + key.fingerprint if key is not None else ""),
            f"queuetruth verify-cert {cert_name} --rerun",
        ],
        "warnings": warnings,
    }
    doc = seal(body, key)
    return Certificate.model_validate(doc)
