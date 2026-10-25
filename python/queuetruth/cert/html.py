"""Self-contained HTML rendering of a `qt.certificate` (optionally with a `qt.cert_verification`).

Built from the report's page blocks (`queuetruth.report.page`: light and dark tokens, status always paired with an
icon and a label). The bytes are a pure function of the documents: no generation time, hostname or path is added.
The page is a reading aid; the JSON certificate is what is signed and verified.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from queuetruth.cert.hashing import canonical_value_text
from queuetruth.cert.models import Certificate, CertVerification
from queuetruth.errors import QtError
from queuetruth.report.fmt import esc
from queuetruth.report.page import CRIT, GOOD, INFO, NEUTRAL, WARN, Raw, badge, callout, code_block, kv, section, shell
from queuetruth.report.page import table as html_table

_STATUS = {"pass": GOOD, "warn": WARN, "fail": CRIT, "skip": NEUTRAL}


def utc(ns: int | None) -> str:
    """`2026-10-24 13:05:00.123456789 UTC` (nanoseconds kept)."""
    if ns is None:
        return "n/a"
    s, frac = divmod(ns, 1_000_000_000)
    try:
        return datetime.fromtimestamp(s, UTC).strftime("%Y-%m-%d %H:%M:%S") + f".{frac:09d} UTC"
    except (OverflowError, ValueError, OSError):  # outside the calendar range: show the asserted value as is
        return f"{ns} ns since the epoch"


def _mono(text: object) -> Raw:
    return Raw(f'<span class="mono">{esc(text)}</span>')


def _short(h: str | None, n: int = 16) -> str:
    return "n/a" if not h else h[:n]


def _claims(cert: Certificate) -> Raw:
    if not cert.claims:
        return Raw("<p>No claims: the certificate records runs, inputs and outputs only.</p>")
    parts = []
    for c in cert.claims:
        rows = [
            [_mono(e.ref), _mono(canonical_value_text(e.value)), _mono(e.file), _mono(e.pointer)] for e in c.evidence
        ]
        ev = (
            html_table(["Reference", "Value", "File", "JSON pointer"], rows)
            if rows
            else Raw('<p class="note">Cites no result value.</p>')
        )
        parts.append(
            f'<div class="why"><h3>{esc(c.id)}: {esc(c.rendered)}</h3>'
            f'<p class="note">As written: <span class="mono">{esc(c.text)}</span></p>{ev}</div>'
        )
    return Raw("".join(parts))


def _verification(v: CertVerification) -> Raw:
    st = GOOD if v.status == "pass" else CRIT
    head = (
        f"Verification {'with' if v.rerun else 'without'} re-execution: {v.counts.pass_} passed, {v.counts.warn} "
        f"warnings, {v.counts.fail} failed, {v.counts.skip} skipped"
    )
    rows = [
        [badge(_STATUS[c.status]), _mono(c.id), c.summary, Raw("<br>".join(esc(d) for d in c.details))]
        for c in v.checks
    ]
    return Raw(
        callout(st, "VERIFIED" if v.status == "pass" else "VERIFICATION FAILED", f"<p>{esc(head)}</p>")
        + html_table(["Status", "Check", "Result", "Details"], rows)
    )


def _runs(cert: Certificate) -> Raw:
    rows = [
        [_mono(r.label), r.kind, _mono(r.run_id), r.binding, _mono(r.path), _mono(r.document), len(r.outputs)]
        for r in cert.runs
    ]
    out = [html_table(["Run", "Kind", "run_id", "Binding", "Directory", "Document", "Outputs"], rows, numeric=[6])]
    for r in cert.runs:
        files = html_table(
            ["File", "Bytes", "sha256", "Normalized sha256"],
            [[_mono(o.path), o.bytes, _mono(o.sha256), _mono(o.normalized_sha256)] for o in r.outputs],
            numeric=[1],
            cls="files",
        )
        ins = ", ".join(f"{ri.pointer} = {ri.input}" for ri in r.inputs) or "none"
        out.append(
            Raw(
                f"<details><summary>{esc(r.label)}: spec, inputs, outputs, reproduction</summary>"
                f'<p>Inputs: <span class="mono">{esc(ins)}</span></p>'
                + code_block(json.dumps(r.spec, indent=2, ensure_ascii=False), "spec echo")
                + code_block(r.reproduce.python, f"Python ({r.reproduce.api})")
                + code_block(r.reproduce.verify, "Verify this run")
                + files
                + "</details>"
            )
        )
    return Raw("".join(out))


def _inputs(items: list, empty: str) -> Raw:
    if not items:
        return Raw(f"<p>{esc(empty)}</p>")
    rows = []
    for x in items:
        consistent = "n/a" if x.dataset_consistent is None else ("yes" if x.dataset_consistent else "NO")
        span = f"{utc(x.data_ts_min)} to {utc(x.data_ts_max)}" if x.data_ts_min is not None else ""
        rows.append([_mono(x.id), x.kind, _mono(x.path), _mono(x.identity), len(x.files), x.bytes, consistent, span])
    return html_table(
        ["Id", "Kind", "Path", "Identity (SPEC 7.3)", "Files", "Bytes", "Manifest consistent", "Data span"],
        rows,
        numeric=[4, 5],
    )


def _commitments(cert: Certificate) -> Raw:
    if not cert.commitments:
        return Raw("<p>No commitment is linked.</p>")
    rows = []
    for c in cert.commitments:
        when = "n/a" if c.precedes_data is None else ("yes" if c.precedes_data else "NO")
        rows.append(
            [
                _mono(c.path),
                c.label or "",
                utc(c.committed_at),
                _mono(_short(c.key_fingerprint)),
                ", ".join(f"{it.name} -> {'/'.join(it.inputs) or 'no input'}" for it in c.revealed),
                utc(c.earliest_data_ts),
                when,
            ]
        )
    return html_table(
        ["Commitment", "Label", "committed_at (asserted)", "Key", "Revealed", "Earliest data", "Before data"], rows
    )


def render(cert: Certificate, verification: CertVerification | None = None) -> str:
    """The complete HTML page."""
    sig = cert.integrity.signature
    title = cert.title or f"queuetruth certificate {cert.certificate_id}"
    if verification is not None and (
        verification.certificate.body_sha256 != cert.integrity.body_sha256
        or verification.certificate.certificate_id != cert.certificate_id
    ):
        raise QtError(
            "QT-INPUT-002",
            f"the verification is of certificate {verification.certificate.certificate_id}, not {cert.certificate_id}",
            "render a certificate with its own qt.cert_verification",
        )
    if sig is None:
        signed = badge(WARN, "UNSIGNED")
    elif verification is None:
        # the page cannot vouch for a signature it did not check
        signed = badge(NEUTRAL, "SIGNATURE NOT VERIFIED")
    else:
        sig_status = next((c.status for c in verification.checks if c.id == "signature"), "fail")
        pin = next((c.status for c in verification.checks if c.id == "key_pin"), None)
        if sig_status != "pass":
            signed = badge(CRIT, "SIGNATURE INVALID")
        elif pin == "pass":
            signed = badge(GOOD, "SIGNED BY A TRUSTED KEY")
        else:
            signed = badge(
                WARN, "SIGNED, KEY NOT PINNED" if pin is None or pin == "warn" else "SIGNED, KEY NOT TRUSTED"
            )
    status = ""
    if verification is not None:
        status = " " + badge(GOOD, "VERIFIED") if verification.status == "pass" else " " + badge(CRIT, "FAILED")
    facts = kv(
        [
            ("Certificate id", _mono(cert.certificate_id)),
            ("Issued (asserted by the issuer)", utc(cert.issued_at)),
            ("Issuer", cert.issuer or "not stated"),
            ("Signing key fingerprint", _mono(sig.key_fingerprint if sig is not None else "unsigned")),
            ("Body sha256", _mono(cert.integrity.body_sha256)),
            ("queuetruth", f"{cert.environment.queuetruth} (engine {cert.environment.engine_version})"),
            ("Runs", len(cert.runs)),
            ("Claims", len(cert.claims)),
        ]
    )
    proves = callout(
        INFO,
        "What a certificate shows",
        "<p>Re-hashing proves the listed files are the ones certified; re-execution proves the outputs follow from "
        "those inputs and specs under this queuetruth version. A signature proves who sealed it only to someone "
        "who trusts that key fingerprint. It does not prove the inputs are real market data or real fills, that the "
        "issue time is true, or that these were the only runs tried.</p>",
    )
    body: list[str] = [
        f'<header><h1>{esc(title)}</h1><p class="sub">{signed}{status} '
        f"queuetruth certificate, schema qt.certificate v1</p>{facts}{proves}</header>"
    ]
    if verification is not None:
        body.append(section("verification", "Verification", [_verification(verification)]))
    body.append(section("claims", "Claims", [_claims(cert)]))
    body.append(section("runs", "Runs", [_runs(cert)]))
    body.append(section("inputs", "Inputs", [_inputs(cert.inputs, "No inputs.")]))
    if cert.attachments:
        body.append(section("attachments", "Attachments", [_inputs(cert.attachments, "No attachments.")]))
    body.append(section("commitments", "Commitments", [_commitments(cert)]))
    env = cert.environment
    env_rows = [
        ("Python", f"{env.python} ({env.implementation})"),
        ("Platform", f"{env.platform} {env.machine}"),
        ("Core", env.core or "not built"),
        *[(k, v) for k, v in sorted(env.packages.items())],
        *[(k, v) for k, v in sorted(env.env.items())],
    ]
    body.append(section("environment", "Environment", [kv(env_rows)]))
    body.append(section("reproduce", "Reproduce", [code_block("\n".join(cert.reproduce), "Commands")]))
    if cert.warnings:
        items = "".join(f"<li>{esc(w)}</li>" for w in cert.warnings)
        body.append(section("warnings", "Warnings", [Raw(f"<ul>{items}</ul>")]))
    return shell(title, "\n".join(body), f"queuetruth certificate {cert.certificate_id}")
