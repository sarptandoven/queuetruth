"""API keys (SPEC 8.5): `qt_live_<32 base62>_<6 base62 CRC32>` (`qt_test_` with ENV=dev).

Only `prefix` (first 12 chars of the random part) and `HMAC-SHA256(KEY_PEPPER, key)` are stored; verification uses
`hmac.compare_digest`. `parse` validates format and checksum offline, so malformed keys never reach the database.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import zlib

B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
KEY_RE = re.compile(r"^qt_(live|test)_([0-9A-Za-z]{32})_([0-9A-Za-z]{6})$")
PREFIX_LEN = 12


def b62(n: int, width: int) -> str:
    """`n` in base62, zero-padded to `width` digits."""
    out = []
    for _ in range(width):
        n, r = divmod(n, 62)
        out.append(B62[r])
    if n:
        raise ValueError("value does not fit")
    return "".join(reversed(out))


def checksum(body: str) -> str:
    """6-char base62 CRC32 of `body` (62**6 > 2**32)."""
    return b62(zlib.crc32(body.encode()) & 0xFFFFFFFF, 6)


def generate(env: str = "dev") -> tuple[str, str]:
    """A new key and its lookup prefix."""
    rand = "".join(secrets.choice(B62) for _ in range(32))
    body = f"qt_{'live' if env == 'prod' else 'test'}_{rand}"
    return f"{body}_{checksum(body)}", rand[:PREFIX_LEN]


def parse(key: str) -> str | None:
    """The lookup prefix of a well-formed key with a valid checksum, else None."""
    m = KEY_RE.match(key)
    if not m:
        return None
    body = key.rsplit("_", 1)[0]
    if not hmac.compare_digest(checksum(body), m.group(3)):
        return None
    return m.group(2)[:PREFIX_LEN]


def key_hash(pepper: bytes, key: str) -> str:
    """Stored hash of a key."""
    return hmac.new(pepper, key.encode(), hashlib.sha256).hexdigest()


def verify(pepper: bytes, key: str, stored_hash: str) -> bool:
    """Constant-time comparison of a key with its stored hash."""
    return hmac.compare_digest(key_hash(pepper, key), stored_hash)


_KEY_RE = re.compile(r"qt_(live|test)_[0-9A-Za-z]{8,}(_[0-9A-Za-z]{0,6})?")
#: Credential-bearing query parameters: local-store `sig`, SigV4 presigned `X-Amz-*`, generic tokens.
_QUERY_SECRET_RE = re.compile(
    r"(?i)((?:^|[?&;\s])(?:sig|signature|x-amz-signature|x-amz-credential|x-amz-security-token|token|access_token"
    r"|api_key|apikey)=)[^&\s\"';]+"
)
_URL_PASSWORD_RE = re.compile(r"(?i)\b((?:postgres(?:ql)?|https?|s3|redis)://[^:/\s@]+:)[^@\s/]+@")
_KV_PASSWORD_RE = re.compile(r"(?i)\b(password\s*=\s*)('[^']*'|[^\s]+)")


def redact(text: str) -> str:
    """Replaces API keys, signed-URL credentials (`sig=`, `X-Amz-Signature=`, ...) and DSN passwords in `text` (logs,
    including uvicorn's access log, which records the query string)."""
    text = _KEY_RE.sub("qt_<redacted>", text)
    text = _QUERY_SECRET_RE.sub(r"\1<redacted>", text)
    text = _URL_PASSWORD_RE.sub(r"\1<redacted>@", text)
    return _KV_PASSWORD_RE.sub(r"\1<redacted>", text)
