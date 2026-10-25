"""Ed25519 keys and signatures (`queuetruth.cert.keys`)."""

from __future__ import annotations

import hashlib
import stat
import sys
from pathlib import Path

import pytest

from queuetruth.cert import keys
from queuetruth.errors import QtError

pytest.importorskip("cryptography")


def test_generate_writes_private_0600_and_public_pem(tmp_path: Path) -> None:
    key, pub, encrypted = keys.generate(tmp_path / "k.pem")
    assert not encrypted
    assert pub == tmp_path / "k.pub.pem"
    assert stat.S_IMODE((tmp_path / "k.pem").stat().st_mode) == 0o600
    assert b"BEGIN PRIVATE KEY" in (tmp_path / "k.pem").read_bytes()
    assert b"BEGIN PUBLIC KEY" in pub.read_bytes()
    assert len(key.public_raw) == 32
    assert key.fingerprint == hashlib.sha256(key.public_raw).hexdigest()
    assert keys.load_public(pub) == key.public_raw
    assert keys.load_public(tmp_path / "k.pem") == key.public_raw  # the public half of a private key
    loaded, enc = keys.load_private(tmp_path / "k.pem")
    assert loaded.fingerprint == key.fingerprint and not enc


def test_generate_refuses_to_overwrite_unless_forced(tmp_path: Path) -> None:
    first, _, _ = keys.generate(tmp_path / "k.pem")
    with pytest.raises(QtError) as e:
        keys.generate(tmp_path / "k.pem")
    assert e.value.code == "QT-USAGE-001"
    second, _, _ = keys.generate(tmp_path / "k.pem", force=True)
    assert second.fingerprint != first.fingerprint
    assert keys.load_private(tmp_path / "k.pem")[0].fingerprint == second.fingerprint


def test_public_path_naming() -> None:
    assert keys.public_path_for(Path("a/key.pem")) == Path("a/key.pub.pem")
    assert keys.public_path_for(Path("a/key")) == Path("a/key.pub.pem")


def test_encrypted_key_needs_the_passphrase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(keys.PASSPHRASE_ENV, "correct horse")
    key, _, encrypted = keys.generate(tmp_path / "e.pem")
    assert encrypted
    assert b"ENCRYPTED" in (tmp_path / "e.pem").read_bytes()
    assert keys.load_private(tmp_path / "e.pem")[0].fingerprint == key.fingerprint
    monkeypatch.setenv(keys.PASSPHRASE_ENV, "wrong")
    with pytest.raises(QtError) as e:
        keys.load_private(tmp_path / "e.pem")
    assert e.value.code == "QT-INPUT-002"
    monkeypatch.delenv(keys.PASSPHRASE_ENV)
    with pytest.raises(QtError) as e:
        keys.load_private(tmp_path / "e.pem")
    assert e.value.code == "QT-CONFIG-001"


def test_non_ed25519_and_garbage_keys_are_refused(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    k = ec.generate_private_key(ec.SECP256R1())
    (tmp_path / "ec.pem").write_bytes(
        k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    (tmp_path / "ec.pub.pem").write_bytes(
        k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    for p in ("ec.pem", "ec.pub.pem"):
        with pytest.raises(QtError, match="not an Ed25519 key"):
            keys.load_public(tmp_path / p)
    (tmp_path / "junk.pem").write_text("-----BEGIN PUBLIC KEY-----\nnope\n-----END PUBLIC KEY-----\n")
    with pytest.raises(QtError) as e:
        keys.load_public(tmp_path / "junk.pem")
    assert e.value.code == "QT-INPUT-002"
    with pytest.raises(QtError) as e:
        keys.load_private(tmp_path / "missing.pem")
    assert e.value.code == "QT-INPUT-001"


def test_sign_and_verify_with_domain_separation(keypair: tuple[Path, Path, str]) -> None:
    key, _ = keys.load_private(keypair[0])
    body = {"schema": "qt.x", "v": [1, 2.5, "z"], "nested": {"b": 1, "a": None}}
    sig = key.sign(keys.CERT_DOMAIN, body)
    assert sig.key_fingerprint == keypair[2]
    assert keys.verify(sig, keys.CERT_DOMAIN, body) == (True, "signature verifies over the body")
    # key order does not matter (canonical JSON), any value change does
    assert keys.verify(sig, keys.CERT_DOMAIN, dict(reversed(list(body.items()))))[0]
    assert not keys.verify(sig, keys.CERT_DOMAIN, {**body, "v": [1, 2.5, "y"]})[0]
    # a certificate signature is never valid as a commitment signature
    assert not keys.verify(sig, keys.COMMIT_DOMAIN, body)[0]
    assert keys.signed_message(keys.CERT_DOMAIN, body).startswith(b"queuetruth.certificate.v1\n{")


def test_verify_rejects_inconsistent_signature_objects(keypair: tuple[Path, Path, str], tmp_path: Path) -> None:
    key, _ = keys.load_private(keypair[0])
    other, _, _ = keys.generate(tmp_path / "other.pem")
    body = {"a": 1}
    sig = key.sign(keys.CERT_DOMAIN, body)
    # a different key claiming the signature: fingerprint mismatch
    forged = sig.model_copy(update={"public_key": other.public_hex})
    ok, why = keys.verify(forged, keys.CERT_DOMAIN, body)
    assert not ok and "fingerprint" in why
    # consistent fingerprint for the other key, but the signature was made by the first key
    swapped = sig.model_copy(update={"public_key": other.public_hex, "key_fingerprint": other.fingerprint})
    assert keys.verify(swapped, keys.CERT_DOMAIN, body) == (False, "signature does not verify over the body")
    assert not keys.verify(sig.model_copy(update={"value": "zz"}), keys.CERT_DOMAIN, body)[0]
    assert not keys.verify(sig.model_copy(update={"value": sig.value[:-2]}), keys.CERT_DOMAIN, body)[0]


def test_normalize_fingerprint() -> None:
    h = "ab" * 32
    for t in (h, h.upper(), f"sha256:{h}", f"ed25519:{h}", f"  {h}\n"):
        assert keys.normalize_fingerprint(t) == h
    for bad in ("ab" * 31, "zz" * 32, ""):
        with pytest.raises(QtError) as e:
            keys.normalize_fingerprint(bad)
        assert e.value.code == "QT-USAGE-001"


def test_missing_cryptography_is_a_usage_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in [m for m in sys.modules if m == "cryptography" or m.startswith("cryptography.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "cryptography", None)
    with pytest.raises(QtError) as e:
        keys.generate(tmp_path / "k.pem")
    assert e.value.code == "QT-USAGE-001"
    assert "cryptography" in (e.value.hint or "")
