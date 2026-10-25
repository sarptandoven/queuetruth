"""Ed25519 keys and signatures for certificates and commitments (the `cryptography` package, imported on use).

Keys are PEM files: the private key as PKCS#8 (encrypted with the passphrase in the environment variable
`QT_CERT_KEY_PASSPHRASE` when it is set, never from a command-line flag), the public key as SubjectPublicKeyInfo.
The key fingerprint is the sha256 of the raw 32-byte public key, in lowercase hex.

A signature covers `domain + "\\n" + canonical_json(body)` (UTF-8). The domain string differs between certificates
and commitments, so a signature over one can never be replayed as the other.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from queuetruth.cert.models import Signature
from queuetruth.errors import QtError
from queuetruth.schemas.runid import canonical_json

PASSPHRASE_ENV = "QT_CERT_KEY_PASSPHRASE"
CERT_DOMAIN = "queuetruth.certificate.v1"
COMMIT_DOMAIN = "queuetruth.commitment.v1"


def _crypto() -> Any:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError:
        raise QtError(
            "QT-USAGE-001",
            "Ed25519 signing and signature checks need the 'cryptography' package",
            'pip install "cryptography>=42"  (unsigned certificates and commitments work without it)',
        ) from None
    return ed25519, serialization


def fingerprint(public_raw: bytes) -> str:
    """sha256 (hex) of the raw 32-byte public key."""
    return hashlib.sha256(public_raw).hexdigest()


def signed_message(domain: str, body: dict[str, Any]) -> bytes:
    """The exact bytes a signature covers."""
    return (domain + "\n" + canonical_json(body)).encode("utf-8")


def _passphrase() -> bytes | None:
    v = os.environ.get(PASSPHRASE_ENV)
    return v.encode("utf-8") if v else None


class SigningKey:
    """A loaded Ed25519 private key."""

    def __init__(self, private: Any) -> None:
        _, serialization = _crypto()
        self._private = private
        self.public_raw: bytes = private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    @property
    def public_hex(self) -> str:
        """Raw public key, hex."""
        return self.public_raw.hex()

    @property
    def fingerprint(self) -> str:
        """sha256 of the raw public key, hex."""
        return fingerprint(self.public_raw)

    def sign(self, domain: str, body: dict[str, Any]) -> Signature:
        """Signs `domain + "\\n" + canonical_json(body)`."""
        sig = self._private.sign(signed_message(domain, body))
        return Signature(public_key=self.public_hex, key_fingerprint=self.fingerprint, value=sig.hex())


def generate(out: Path, *, force: bool = False) -> tuple[SigningKey, Path, bool]:
    """Writes a new private key to `out` (mode 0600) and its public key to `<out stem>.pub.pem`; returns the key,
    the public key path and whether the private key is encrypted. Refuses to overwrite unless `force`."""
    ed25519, serialization = _crypto()
    pub_path = public_path_for(out)
    for p in (out, pub_path):
        if p.exists() and not force:
            raise QtError("QT-USAGE-001", f"{p}: already exists", "pass --force to replace it (the old key is lost)")
    private = ed25519.Ed25519PrivateKey.generate()
    pw = _passphrase()
    enc = serialization.BestAvailableEncryption(pw) if pw else serialization.NoEncryption()
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    key = SigningKey(private)
    pub_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    pub_path.write_bytes(pub_pem)
    return key, pub_path, pw is not None


def public_path_for(private_path: Path) -> Path:
    """`key.pem` -> `key.pub.pem`; `key` -> `key.pub.pem`."""
    name = private_path.name
    stem = name[: -len(".pem")] if name.endswith(".pem") else name
    return private_path.with_name(stem + ".pub.pem")


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as e:
        raise QtError("QT-INPUT-001", f"{path}: {e.strerror or e}") from None


def load_private(path: Path) -> tuple[SigningKey, bool]:
    """Loads a PEM private key (decrypted with `QT_CERT_KEY_PASSPHRASE` when encrypted); returns (key, encrypted)."""
    ed25519, serialization = _crypto()
    data = _read(path)
    encrypted = b"ENCRYPTED" in data
    pw = _passphrase()
    if encrypted and pw is None:
        raise QtError("QT-CONFIG-001", f"{path}: the private key is encrypted", f"set {PASSPHRASE_ENV}")
    try:
        private = serialization.load_pem_private_key(data, password=pw if encrypted else None)
    except (ValueError, TypeError) as e:
        raise QtError("QT-INPUT-002", f"{path}: not a usable PEM private key ({e})") from None
    if not isinstance(private, ed25519.Ed25519PrivateKey):
        raise QtError("QT-INPUT-002", f"{path}: not an Ed25519 key ({type(private).__name__})")
    return SigningKey(private), encrypted


def load_public(path: Path) -> bytes:
    """Raw 32-byte public key from a PEM public key, or from a PEM private key (its public half)."""
    ed25519, serialization = _crypto()
    data = _read(path)
    if b"PRIVATE KEY" in data:
        return load_private(path)[0].public_raw
    try:
        public = serialization.load_pem_public_key(data)
    except (ValueError, TypeError) as e:
        raise QtError("QT-INPUT-002", f"{path}: not a usable PEM public key ({e})") from None
    if not isinstance(public, ed25519.Ed25519PublicKey):
        raise QtError("QT-INPUT-002", f"{path}: not an Ed25519 key ({type(public).__name__})")
    return public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def verify(sig: Signature, domain: str, body: dict[str, Any]) -> tuple[bool, str]:
    """(valid, reason). Checks that the fingerprint matches the embedded key and that the signature verifies."""
    ed25519, _ = _crypto()
    from cryptography.exceptions import InvalidSignature

    try:
        raw = bytes.fromhex(sig.public_key)
        value = bytes.fromhex(sig.value)
    except ValueError:
        return False, "public key or signature is not hex"
    if len(raw) != 32 or len(value) != 64:
        return False, f"public key must be 32 bytes and signature 64 bytes (got {len(raw)} and {len(value)})"
    if fingerprint(raw) != sig.key_fingerprint:
        return False, "key_fingerprint is not the sha256 of public_key"
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(raw).verify(value, signed_message(domain, body))
    except InvalidSignature:
        return False, "signature does not verify over the body"
    return True, "signature verifies over the body"


def normalize_fingerprint(text: str) -> str:
    """Accepts `HEX64`, `sha256:HEX64` or `ed25519:HEX64` (case-insensitive); returns lowercase hex."""
    t = text.strip().lower()
    for prefix in ("sha256:", "ed25519:"):
        t = t.removeprefix(prefix)
    if len(t) != 64 or any(c not in "0123456789abcdef" for c in t):
        raise QtError("QT-USAGE-001", f"--trust {text!r}: expected a 64-hex-digit key fingerprint")
    return t
