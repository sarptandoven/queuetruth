"""JSON documents of `queuetruth certify`, `verify-cert`, `commit`, `reveal` and `cert keygen` (Pydantic v2 models,
the source of truth for `schemas/json/qt.certificate*.schema.json` and the other `qt.*` names below).

- `qt.certificate`: what was run (specs, run ids, engine and package versions, environment), on which inputs (sha256
  of every input file, SPEC 7.3 identities), what came out (sha256 of every output file, raw and normalized), which
  claims rest on which exact result values, and how to reproduce it. `integrity` holds the sha256 of the canonical
  body (RFC 8785 with integers written exactly, SPEC 7.3) and, when signed, an Ed25519 signature over it.
- `qt.cert_verification`: the checks of `verify-cert` (integrity, signature, key pin, inputs, outputs, run binding,
  claims, commitments and, with `--rerun`, bit-identical re-execution).
- `qt.commitment` / `qt.commitment_opening` / `qt.commitment_verification`: salted hash commitments to files
  (commit now, reveal later), with selective per-file opening.
- `qt.signing_key`: the public half of an Ed25519 key written by `cert keygen` or loaded by `cert pubkey`.

The body of a certificate or a commitment is the document without its integrity part (`certificate_id` and
`integrity`; `signature`); `queuetruth.cert.keys` signs `DOMAIN + "\\n" + canonical_json(body)`.

This module imports only Pydantic and `queuetruth.schemas.models` base classes, so `queuetruth.schemas.models` can
register these documents in `DOC_MODELS` at import time.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field

from queuetruth.schemas.models import HEX16, HEX64, QtDoc, QtModel

Sha256 = Annotated[str, Field(pattern=HEX64)]
Hex16 = Annotated[str, Field(pattern=HEX16)]
#: Raw Ed25519 public key (32 bytes) as lowercase hex.
PublicKeyHex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
#: Raw Ed25519 signature (64 bytes) as 128 lowercase hex characters (checked when verifying, not by the schema).
SignatureHex = str
#: Run label: 1 to 64 of `[A-Za-z0-9_.-]`, not starting with a dot (so never `.` or `..`): a label names the
#: directory `verify-cert --rerun` re-executes the run into, so it must be one plain path component.
LABEL_PATTERN = r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$"
Label = Annotated[str, Field(pattern=LABEL_PATTERN)]
#: One path component other than `.` and `..` (no `/`).
_COMPONENT = r"(?:[^/.][^/]*|\.[^/.][^/]*|\.\.[^/]+)"
#: A relative POSIX path inside a run or input directory: components separated by `/`, none empty, `.` or `..`, no
#: leading `/`. Files named by a certificate can never point outside the directory they belong to.
REL_PATH_PATTERN = rf"^{_COMPONENT}(?:/{_COMPONENT})*$"
RelPath = Annotated[str, Field(pattern=REL_PATH_PATTERN)]
CheckStatus = Literal["pass", "warn", "fail", "skip"]
RunKind = Literal["replay", "zoo", "calibrate", "calibrate_live", "fit_hazard", "score", "referee", "dq"]


def _tag(name: str) -> Any:
    return Field(default=name, alias="schema")


# ---- shared parts -----------------------------------------------------------------------------------------------


class Signature(QtModel):
    """An Ed25519 signature over `domain + "\\n" + canonical_json(body)` (RFC 8032, pure Ed25519)."""

    algorithm: Literal["ed25519"] = "ed25519"
    public_key: PublicKeyHex
    #: sha256 of the raw 32-byte public key (lowercase hex); the value to pin with `verify-cert --trust`.
    key_fingerprint: Sha256
    value: SignatureHex


class HashedFile(QtModel):
    """One file: path relative to its input or run directory (POSIX separators), size and sha256 of the bytes."""

    path: RelPath
    bytes: int
    sha256: Sha256


class Check(QtModel):
    """One verification check: `fail` makes the verdict fail; `warn` does not."""

    id: str
    status: CheckStatus
    summary: str
    details: list[str] = Field(default_factory=list)


class CheckCounts(QtModel):
    """Checks per status."""

    pass_: int = Field(alias="pass")
    warn: int
    fail: int
    skip: int


# ---- qt.certificate ---------------------------------------------------------------------------------------------


class CertInput(QtModel):
    """An input of one or more runs (or an attachment): every file it consists of, hashed.

    `identity` is the SPEC 7.3 reference identity that enters `run_id` (`dataset_id` for a dataset, the sha256 of
    `manifest.json` for a live-log directory, the file sha256 for a file). `files` lists every file (one entry,
    named after the file, for a file input). For a dataset, `dataset_consistent` says that the partition hashes of
    its `manifest.json` match the files and re-derive its `dataset_id`."""

    #: `in<N>` for run inputs, `att<N>` for attachments.
    id: str
    path: str
    kind: Literal["dataset", "directory", "file"]
    identity: Sha256
    bytes: int
    files: list[HashedFile]
    dataset_id: Sha256 | None = None
    dataset_consistent: bool | None = None
    #: Earliest and latest `ts_event` over the dataset's partitions (manifest `ts_min` / `ts_max`, integer ns).
    data_ts_min: int | None = None
    data_ts_max: int | None = None


class RunInput(QtModel):
    """A reference of a run's spec: JSON pointer into the spec echo and the certificate input it names."""

    pointer: str
    input: str


class OutputFile(QtModel):
    """An output file of a run. `normalized_sha256` is what `--rerun` compares (SPEC 7.2): JSON files canonicalised
    (RFC 8785) without `timings` at any depth and without the execution-only `out` / `threads` of the top-level
    `spec` echo; other files hashed as bytes."""

    path: RelPath
    bytes: int
    sha256: Sha256
    normalized_sha256: Sha256


class Reproduce(QtModel):
    """How to re-execute one run: the Python API call and the verification command."""

    api: str
    python: str
    verify: str


class CertRun(QtModel):
    """One certified run directory."""

    label: Label
    kind: RunKind
    path: str
    #: The run's summary document, relative to `path` (`summary.json`, `calibration.json`, ...).
    document: RelPath
    #: `schema` of that document.
    document_schema: str
    #: `run_id` recomputed from the spec echo, the engine version and the input identities (SPEC 7.3).
    run_id: Hex16
    #: `verified`: the document records a run id (or `model_id` suffix) equal to `run_id`; `recomputed`: the
    #: document records none (referee, dq), so the binding of inputs to outputs rests on `--rerun` alone.
    binding: Literal["verified", "recomputed"]
    engine_version: str
    spec: dict[str, Any]
    inputs: list[RunInput]
    outputs: list[OutputFile]
    reproduce: Reproduce


class Evidence(QtModel):
    """One result value a claim rests on: `{run}[:{file}]#{pointer}` resolved at certification time."""

    ref: str
    run: Label
    #: An output of `run` (one of its `outputs[].path`).
    file: RelPath
    pointer: str
    value: Any


class Claim(QtModel):
    """A claim: the text as given (with `{run#/pointer}` references), the text with the values filled in, and the
    exact values."""

    #: `c<N>` in command-line order.
    id: str
    text: str
    rendered: str
    evidence: list[Evidence]


class RevealedItem(QtModel):
    """A committed file revealed by an opening and matched (by sha256) to certificate inputs."""

    index: int
    name: str
    sha256: Sha256
    #: The item's nonce: revealed anyway, and kept so that `verify-cert` can recompute the leaf.
    nonce: Sha256
    inputs: list[str]


class CommitmentLink(QtModel):
    """A `qt.commitment` linked to the certificate through an opening of some of its items."""

    path: str
    sha256: Sha256
    root: Sha256
    label: str | None
    committed_at: int
    key_fingerprint: Sha256 | None
    revealed: list[RevealedItem]
    #: Earliest `ts_event` of the datasets of the runs that use a revealed item (null when no run does).
    earliest_data_ts: int | None
    #: `committed_at < earliest_data_ts` (null when not comparable). As trustworthy as `committed_at`, which is
    #: asserted by whoever ran `commit` unless the commitment root was anchored elsewhere.
    precedes_data: bool | None


class Environment(QtModel):
    """Software and platform of the certifying run (no hostname, user name or absolute path)."""

    queuetruth: str
    core: str | None
    engine_version: str
    python: str
    implementation: str
    platform: str
    machine: str
    packages: dict[str, str]
    #: Environment variables that may affect numeric libraries (only those set).
    env: dict[str, str]


class Integrity(QtModel):
    """`body_sha256` = sha256 of `canonical_json(body)`; `signature` signs `domain + "\\n" + body`.

    `rfc8785` names queuetruth's canonical JSON (SPEC 7.3): RFC 8785 except that integers are written exactly as
    integers. `issued_at`, `committed_at` and `data_ts_*` are ns values above 2^53, which a JCS library that writes
    every number as a double would round, so an independent verifier must keep integers exact."""

    canonicalization: Literal["rfc8785"] = "rfc8785"
    domain: Literal["queuetruth.certificate.v1"] = "queuetruth.certificate.v1"
    body_sha256: Sha256
    signature: Signature | None = None


class Certificate(QtDoc):
    """`qt.certificate`: a rerunnable record of runs, inputs, outputs and claims (`queuetruth certify`)."""

    SCHEMA: ClassVar[str] = "qt.certificate"
    schema_: Literal["qt.certificate"] = _tag("qt.certificate")
    schema_version: Literal[1] = 1
    #: First 16 hex characters of `integrity.body_sha256`.
    certificate_id: Hex16
    title: str | None = None
    issuer: str | None = None
    #: When the certificate was issued (integer ns, UTC; `SOURCE_DATE_EPOCH` when set). Asserted by the issuer.
    issued_at: int
    environment: Environment
    runs: list[CertRun]
    inputs: list[CertInput]
    claims: list[Claim]
    attachments: list[CertInput] = Field(default_factory=list)
    commitments: list[CommitmentLink] = Field(default_factory=list)
    reproduce: list[str]
    warnings: list[str] = Field(default_factory=list)
    integrity: Integrity


# ---- qt.cert_verification ---------------------------------------------------------------------------------------


class VerifiedCertificate(QtModel):
    """What was verified."""

    path: str
    certificate_id: Hex16 | None
    body_sha256: Sha256 | None
    issued_at: int | None
    signed: bool
    key_fingerprint: Sha256 | None
    #: The signature verifies and its key matches a `--trust` fingerprint or `--pubkey` (null when none was given).
    key_trusted: bool | None


class RunVerification(QtModel):
    """Per-run outcome: `rerun` is `identical`, `equivalent` (every output identical or, for JSON and Parquet,
    equal within the SPEC 7.2 cross-platform tolerance of 4 ulp on floating-point values), `differs`, `failed`,
    `skipped` (an integrity, signature or key-pin check failed) or `not_run`."""

    label: str
    kind: str
    run_id: str | None
    inputs_ok: bool
    outputs_ok: bool
    binding_ok: bool
    rerun: Literal["identical", "equivalent", "differs", "failed", "skipped", "not_run"]
    differing: list[str] = Field(default_factory=list)
    #: Outputs whose bytes differ but whose values agree within 4 ulp (only when `rerun` is `equivalent`).
    within_tolerance: list[str] = Field(default_factory=list)


class CertVerification(QtDoc):
    """`qt.cert_verification` (`queuetruth verify-cert`): `status` is `fail` when any check failed."""

    SCHEMA: ClassVar[str] = "qt.cert_verification"
    schema_: Literal["qt.cert_verification"] = _tag("qt.cert_verification")
    schema_version: Literal[1] = 1
    certificate: VerifiedCertificate
    rerun: bool
    status: Literal["pass", "fail"]
    counts: CheckCounts
    checks: list[Check]
    runs: list[RunVerification]
    warnings: list[str] = Field(default_factory=list)


# ---- commitments -------------------------------------------------------------------------------------------------


class Commitment(QtDoc):
    """`qt.commitment` (`queuetruth commit`): public, reveals nothing about the files but their number.

    Leaf i = sha256(`"qt-commit-v1\\0leaf\\0"` + nonce_i (32 bytes) + name_i (UTF-8) + `"\\0"` + sha256(file_i) (32
    bytes)); root = sha256(`"qt-commit-v1\\0root\\0"` + leaf_0 + ... + leaf_n-1). The nonces stay in the private
    `qt.commitment_opening` until reveal. `committed_at` is asserted by whoever ran `commit`; anchor `root`
    elsewhere (RFC 3161, a public post, a counterparty) to make it more than that."""

    SCHEMA: ClassVar[str] = "qt.commitment"
    schema_: Literal["qt.commitment"] = _tag("qt.commitment")
    schema_version: Literal[1] = 1
    scheme: Literal["qt-commit-v1"] = "qt-commit-v1"
    label: str | None = None
    committed_at: int
    n_items: int
    leaves: list[Sha256]
    root: Sha256
    signature: Signature | None = None


class OpeningItem(QtModel):
    """The secret of one committed file."""

    index: int
    name: str
    bytes: int
    sha256: Sha256
    nonce: Sha256


class CommitmentOpening(QtDoc):
    """`qt.commitment_opening`: keep private until reveal; any subset of `items` opens those items alone."""

    SCHEMA: ClassVar[str] = "qt.commitment_opening"
    schema_: Literal["qt.commitment_opening"] = _tag("qt.commitment_opening")
    schema_version: Literal[1] = 1
    root: Sha256
    items: list[OpeningItem]


class VerifiedCommitment(QtModel):
    """The commitment that was checked."""

    path: str
    root: Sha256 | None
    label: str | None
    committed_at: int | None
    n_items: int | None
    signed: bool
    key_fingerprint: Sha256 | None
    key_trusted: bool | None


class RevealOutcome(QtModel):
    """One opened item: `match` (leaf and file agree), `leaf_mismatch` (the opening does not open this leaf),
    `content_mismatch` (no given file has the committed content) or `not_given` (no file given for it)."""

    index: int
    name: str
    sha256: Sha256
    status: Literal["match", "leaf_mismatch", "content_mismatch", "not_given"]
    file: str | None


class CommitmentVerification(QtDoc):
    """`qt.commitment_verification` (`queuetruth reveal`)."""

    SCHEMA: ClassVar[str] = "qt.commitment_verification"
    schema_: Literal["qt.commitment_verification"] = _tag("qt.commitment_verification")
    schema_version: Literal[1] = 1
    commitment: VerifiedCommitment
    status: Literal["pass", "fail"]
    n_revealed: int
    revealed: list[RevealOutcome]
    unmatched_files: list[str]
    checks: list[Check]


# ---- qt.signing_key ----------------------------------------------------------------------------------------------


class SigningKey(QtDoc):
    """`qt.signing_key` (`queuetruth cert keygen` / `cert pubkey`): never carries private key material."""

    SCHEMA: ClassVar[str] = "qt.signing_key"
    schema_: Literal["qt.signing_key"] = _tag("qt.signing_key")
    schema_version: Literal[1] = 1
    algorithm: Literal["ed25519"] = "ed25519"
    public_key: PublicKeyHex
    key_fingerprint: Sha256
    private_key_path: str | None
    public_key_path: str | None
    #: The private key file is encrypted with `QT_CERT_KEY_PASSPHRASE` (null when no private key is involved).
    encrypted: bool | None


CERT_DOC_MODELS: dict[str, type[QtDoc]] = {
    m.SCHEMA: m
    for m in (
        Certificate,
        CertVerification,
        Commitment,
        CommitmentOpening,
        CommitmentVerification,
        SigningKey,
    )
}

__all__ = [
    "CERT_DOC_MODELS",
    "CertInput",
    "CertRun",
    "CertVerification",
    "Certificate",
    "Check",
    "CheckCounts",
    "Claim",
    "Commitment",
    "CommitmentLink",
    "CommitmentOpening",
    "CommitmentVerification",
    "Environment",
    "Evidence",
    "HashedFile",
    "Integrity",
    "OpeningItem",
    "OutputFile",
    "Reproduce",
    "RevealOutcome",
    "RevealedItem",
    "RunInput",
    "RunVerification",
    "Signature",
    "SigningKey",
    "VerifiedCertificate",
    "VerifiedCommitment",
]
