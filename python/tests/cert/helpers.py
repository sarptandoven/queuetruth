"""Helpers of the certificate tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

from queuetruth.cert.certify import certificate_body, certify, seal
from queuetruth.cert.models import Certificate, CertVerification
from queuetruth.cert.verify import verify_certificate

REPO = Path(__file__).resolve().parents[3]
CLAIMS = [
    "replay filled {replay#/shadows/n_filled} of {replay#/shadows/n} shadows",
    "referee verdict {referee#/pass} with {referee:referee_report.json#/discordant/sim_only} sim-only fills",
    "dq status {dq#/status}",
]


def make_cert(runs: tuple[str, ...] = ("replay", "referee", "dq"), **kw: Any) -> Certificate:
    """Certifies runs of the `work` fixture (cwd) with the standard claims unless given."""
    kw.setdefault("claims", CLAIMS)
    return certify(list(runs), **kw)


def write(cert: Certificate | dict[str, Any], path: Path) -> Path:
    doc = cert.model_dump(mode="json") if isinstance(cert, Certificate) else cert
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def reseal(doc: dict[str, Any], key: Any = None) -> dict[str, Any]:
    """What a forger does after editing an unsigned certificate: recompute the digest (and sign with own key)."""
    return seal(certificate_body(doc), key)


def status(ver: CertVerification, check_id: str) -> str:
    """Status of one check (`KeyError` when absent)."""
    hits = [c.status for c in ver.checks if c.id == check_id]
    if not hits:
        raise KeyError(f"{check_id} not in {[c.id for c in ver.checks]}")
    return hits[0]


def failed(ver: CertVerification) -> list[str]:
    return [c.id for c in ver.checks if c.status == "fail"]


def verify(path: Path, **kw: Any) -> CertVerification:
    return verify_certificate(path, **kw)


def schema_valid(doc: dict[str, Any]) -> None:
    """Validates a document against its committed JSON Schema."""
    name = doc["schema"]
    schema = json.loads((REPO / "schemas" / "json" / f"{name}.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(doc)
