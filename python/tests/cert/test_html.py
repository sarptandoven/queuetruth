"""The HTML page of a certificate (`queuetruth.cert.html`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cert.helpers import make_cert, verify, write
from queuetruth.cert.html import render, utc

pytestmark = pytest.mark.requires_core


def test_utc_keeps_nanoseconds() -> None:
    assert utc(1_700_000_000_123_456_789) == "2023-11-14 22:13:20.123456789 UTC"
    assert utc(None) == "n/a"


def test_page_is_escaped_complete_and_deterministic(work: Path) -> None:
    cert = make_cert(
        claims=['<script>alert("x")</script> filled {replay#/shadows/n_filled}'],
        title="Q4 <b>desk</b> review",
        issuer="A & B",
    )
    page = render(cert)
    assert page == render(cert)
    assert "<script>alert" not in page and "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page
    assert "Q4 &lt;b&gt;desk&lt;/b&gt; review" in page and "A &amp; B" in page
    assert cert.certificate_id in page and cert.integrity.body_sha256 in page
    assert "prefers-color-scheme: dark" in page
    for sid in ("claims", "runs", "inputs", "commitments", "environment", "reproduce"):
        assert f'id="{sid}"' in page
    assert "UNSIGNED" in page and "What a certificate shows" in page


def test_page_with_verification(work: Path) -> None:
    cert = make_cert(("replay",), claims=[])
    ver = verify(write(cert, work.parent / "c.json"))
    page = render(cert, ver)
    assert 'id="verification"' in page and "VERIFIED" in page
    assert "integrity" in page


def test_utc_outside_the_calendar_shows_the_raw_value() -> None:
    assert utc(10**30) == f"{10**30} ns since the epoch"
    assert utc(-(10**30)) == f"{-(10**30)} ns since the epoch"


def test_signature_badge_says_what_was_checked(work: Path, keypair: tuple[Path, Path, str]) -> None:
    from queuetruth.cert import keys

    key, _ = keys.load_private(keypair[0])
    cert = make_cert(("replay",), claims=[], key=key)
    assert "SIGNATURE NOT VERIFIED" in render(cert)
    p = write(cert, work.parent / "c.json")
    assert "SIGNED, KEY NOT PINNED" in render(cert, verify(p))
    assert "SIGNED BY A TRUSTED KEY" in render(cert, verify(p, trust=[keypair[2]]))
