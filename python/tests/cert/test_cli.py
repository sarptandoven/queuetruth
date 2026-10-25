"""The certificate commands through `queuetruth.cli.main` (exit codes, JSON output, human output, errors)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cert.helpers import schema_valid
from queuetruth.cert.cli import command_word, maybe_main
from queuetruth.cli import main

pytestmark = pytest.mark.requires_core


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_command_word() -> None:
    assert command_word(["certify", "x"]) == "certify"
    assert command_word(["--format", "json", "--quiet", "verify-cert", "c.json"]) == "verify-cert"
    assert command_word(["--log-level", "debug", "--seed", "3", "replay"]) == "replay"
    assert command_word(["--format=json", "cert", "keygen"]) == "cert"
    assert command_word(["--verbose"]) is None
    assert maybe_main(["replay", "--help"]) is None  # left to the main grammar


def test_certify_and_verify_json(work: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cert = str(work.parent / "cert.json")
    rc, out, err = _run(
        capsys,
        "--format",
        "json",
        "certify",
        "replay",
        "referee",
        "--claim",
        "{replay#/shadows/n_filled} filled",
        "--out",
        cert,
        "--html",
        str(work.parent / "cert.html"),
        "--title",
        "demo",
        "--issuer",
        "tests",
    )
    assert rc == 0, err
    doc = json.loads(out)
    schema_valid(doc)
    assert doc == json.loads(Path(cert).read_text())
    assert doc["title"] == "demo" and doc["issuer"] == "tests"
    assert "records no run_id" in err  # warnings go to stderr
    assert (work.parent / "cert.html").read_text().startswith("<!doctype html>")
    rc, out, err = _run(capsys, "verify-cert", cert, "--format", "json", "--out", str(work.parent / "v.json"))
    assert rc == 0, err
    v = json.loads(out)
    schema_valid(v)
    assert v["status"] == "pass" and json.loads((work.parent / "v.json").read_text()) == v


def test_human_output_and_failed_verification_exit_1(work: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cert = str(work.parent / "cert.json")
    rc, out, _ = _run(capsys, "certify", "replay", "--out", cert, "--claim", "{replay#/shadows/n} shadows")
    assert rc == 0
    first = out.splitlines()[0]
    assert first.startswith(f"certificate {cert}: 1 run(s)") and "unsigned" in first
    assert "  c1: 20 shadows" in out
    rc, out, err = _run(capsys, "verify-cert", cert)
    assert rc == 0 and out.startswith("VERIFIED: certificate ")
    (work / "replay" / "bbo.parquet").write_bytes(b"x")
    rc, out, err = _run(capsys, "verify-cert", cert, "--html", str(work.parent / "v.html"))
    assert rc == 1
    assert out.startswith("FAILED: ") and "FAIL outputs:replay" in out and "changed: bbo.parquet" in out
    assert "QT-GATE-001" in err
    assert "VERIFICATION FAILED" in (work.parent / "v.html").read_text()
    rc, out, _ = _run(capsys, "--format", "json", "verify-cert", cert, "--quiet")
    assert rc == 1 and json.loads(out)["status"] == "fail"


def test_rerun_through_the_cli(work: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cert = str(work.parent / "cert.json")
    assert _run(capsys, "certify", "replay", "--out", cert)[0] == 0
    rc, out, err = _run(capsys, "verify-cert", cert, "--rerun", "--verbose")
    assert rc == 0, out + err
    assert "with re-execution" in out and "PASS rerun:replay" in out


def test_usage_errors_exit_2(work: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out, _ = _run(capsys, "--format", "json", "certify", "replay")  # no --out
    assert rc == 2
    err = json.loads(out)
    schema_valid(err)
    assert err["code"] == "QT-USAGE-001"
    rc, out, _ = _run(capsys, "--format", "json", "certify", "fx", "--out", "c.json")
    assert rc == 2 and json.loads(out)["code"] == "QT-INPUT-002"
    rc, _, err = _run(capsys, "certify", "replay", "--out", "c.json", "--commitment", "a.json")
    assert rc == 2 and "one --opening per --commitment" in err
    rc, _, err = _run(capsys, "verify-cert", "missing.json")
    assert rc == 2 and err.startswith("QT-INPUT-001")
    rc, _, err = _run(capsys, "verify-cert", "x.json", "--map", "nonsense")
    assert rc == 2
    rc, _, err = _run(capsys, "cert", "frobnicate")
    assert rc == 2 and "QT-USAGE-001" in err
    rc, out, _ = _run(capsys, "certify", "--help")
    assert rc == 0 and "--claim" in out


def test_keys_commit_reveal_and_render(work: Path, capsys: pytest.CaptureFixture[str]) -> None:
    key = str(work.parent / "k" / "signer.pem")
    rc, out, _ = _run(capsys, "--format", "json", "cert", "keygen", "--out", key)
    assert rc == 0
    kdoc = json.loads(out)
    schema_valid(kdoc)
    assert kdoc["private_key_path"] == key and kdoc["encrypted"] is False
    assert "PRIVATE" not in out
    rc, out, _ = _run(capsys, "cert", "keygen", "--out", key)
    assert rc == 2  # refuses to overwrite
    rc, out, _ = _run(capsys, "--format", "json", "cert", "pubkey", kdoc["public_key_path"])
    assert rc == 0 and json.loads(out)["key_fingerprint"] == kdoc["key_fingerprint"]
    rc, out, _ = _run(capsys, "cert", "pubkey", key)
    assert rc == 0 and kdoc["key_fingerprint"] in out

    c, o = str(work.parent / "c.json"), str(work.parent / "o.json")
    rc, out, _ = _run(capsys, "commit", "shadows.parquet", "--out", c, "--opening", o, "--key", key, "--label", "s")
    assert rc == 0 and "private opening" in out
    rc, out, _ = _run(capsys, "--format", "json", "reveal", c, "shadows.parquet", "--opening", o, "--trust",
                      kdoc["key_fingerprint"])  # fmt: skip
    assert rc == 0 and json.loads(out)["status"] == "pass"
    rc, out, err = _run(capsys, "reveal", c, "fx/schedule.parquet", "--opening", o)
    assert rc == 1 and out.startswith("FAILED") and "QT-GATE-001" in err

    cert = str(work.parent / "cert.json")
    rc, _, _ = _run(capsys, "certify", "replay", "--out", cert, "--key", key, "--commitment", c, "--opening", o)
    assert rc == 0
    rc, out, _ = _run(capsys, "verify-cert", cert, "--pubkey", kdoc["public_key_path"])
    assert rc == 0 and "(trusted)" in out
    rc, out, _ = _run(capsys, "verify-cert", cert, "--format", "json", "--out", str(work.parent / "v.json"))
    html = str(work.parent / "page.html")
    rc, out, _ = _run(capsys, "--format", "json", "cert", "render", cert, "--out", html, "--verification",
                      str(work.parent / "v.json"))  # fmt: skip
    assert rc == 0
    summary = json.loads(out)
    schema_valid(summary)
    assert summary["sections"] == ["certificate", "verification"]
    assert summary["bytes"] == len(Path(html).read_bytes())


def test_encrypted_key_through_the_cli(
    work: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QT_CERT_KEY_PASSPHRASE", "s3cret")
    key = str(work.parent / "enc.pem")
    rc, out, _ = _run(capsys, "cert", "keygen", "--out", key)
    assert rc == 0 and "encrypted)" in out
    assert _run(capsys, "certify", "replay", "--out", str(work.parent / "c.json"), "--key", key)[0] == 0
    monkeypatch.delenv("QT_CERT_KEY_PASSPHRASE")
    rc, _, err = _run(capsys, "certify", "replay", "--out", str(work.parent / "c.json"), "--key", key)
    assert rc == 2 and "QT-CONFIG-001" in err


def test_global_flags_help_and_hints(work: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cert = str(work.parent / "cert.json")
    assert _run(capsys, "--threads", "2", "--seed", "7", "certify", "replay", "--out", cert)[0] == 0
    rc, out, err = _run(capsys, "--threads", "2", "verify-cert", cert, "--rerun", "--timeout", "600")
    assert rc == 0, out + err
    rc, _, err = _run(capsys, "verify-cert", cert, "--threads", "0")
    assert rc == 2 and "positive integer" in err
    rc, _, err = _run(capsys, "verify-cert", cert, "--rerun", "--timeout", "0")
    assert rc == 2 and "--timeout" in err
    rc, out, _ = _run(capsys, "help", "verify-cert")
    assert rc == 0 and "--force-rerun" in out
    rc, out, _ = _run(capsys, "help", "cert", "partial-opening")
    assert rc == 0 and "--item" in out
    assert maybe_main(["help", "replay"]) is None
    rc, _, err = _run(capsys, "certify")
    assert rc == 2 and "hint: queuetruth certify --help" in err and "queuetruth queuetruth" not in err
    rc, _, err = _run(capsys, "cert", "keygen")
    assert rc == 2 and "hint: queuetruth cert keygen --help" in err


def test_commit_never_overwrites_an_opening(work: Path, capsys: pytest.CaptureFixture[str]) -> None:
    c, o = work.parent / "c.json", work.parent / "o.json"
    assert (
        _run(capsys, "commit", "shadows.parquet", "fx/schedule.parquet", "--out", str(c), "--opening", str(o))[0] == 0
    )
    first = o.read_bytes()
    rc, _, err = _run(capsys, "commit", "shadows.parquet", "--out", str(c), "--opening", str(o))
    assert rc == 2 and "--force" in err and o.read_bytes() == first
    # a partial opening reveals one file and keeps the other hidden
    part = work.parent / "part.json"
    rc, out, _ = _run(capsys, "cert", "partial-opening", str(o), "--item", "schedule.parquet", "--out", str(part))
    assert rc == 0 and "opens 1 item(s): schedule.parquet" in out
    doc = json.loads(part.read_text())
    schema_valid(doc)
    assert [it["name"] for it in doc["items"]] == ["schedule.parquet"] and doc["items"][0]["index"] == 1
    rc, out, _ = _run(capsys, "--format", "json", "reveal", str(c), "fx/schedule.parquet", "--opening", str(part))
    assert rc == 0 and json.loads(out)["n_revealed"] == 1
    rc, _, err = _run(
        capsys, "cert", "partial-opening", str(o), "--item", "nope", "--out", str(work.parent / "p2.json")
    )
    assert rc == 2 and "no committed item named 'nope'" in err
    rc, _, _ = _run(capsys, "cert", "partial-opening", str(o), "--item", "shadows.parquet", "--out", str(part))
    assert rc == 2  # exists
    assert _run(capsys, "commit", "shadows.parquet", "--out", str(c), "--opening", str(o), "--force")[0] == 0
    assert o.read_bytes() != first


def test_render_refuses_a_verification_of_another_certificate(work: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a, b = str(work.parent / "a.json"), str(work.parent / "b.json")
    assert _run(capsys, "certify", "replay", "--out", a)[0] == 0
    assert _run(capsys, "certify", "referee", "--out", b)[0] == 0
    v = str(work.parent / "va.json")
    assert _run(capsys, "verify-cert", a, "--out", v)[0] == 0
    rc, _, err = _run(capsys, "cert", "render", b, "--out", str(work.parent / "b.html"), "--verification", v)
    assert rc == 2 and "QT-INPUT-002" in err and not (work.parent / "b.html").exists()
