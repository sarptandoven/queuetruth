"""Signed, rerunnable certificates for results and claims, and timestamped hash commitments.

- `queuetruth.cert.certify.certify(...)` -> `qt.certificate` (`queuetruth certify`)
- `queuetruth.cert.verify.verify_certificate(...)` -> `qt.cert_verification` (`queuetruth verify-cert [--rerun]`)
- `queuetruth.cert.commit.commit(...)` / `reveal(...)` -> `qt.commitment`, `qt.commitment_opening`,
  `qt.commitment_verification` (`queuetruth commit` / `reveal`)
- `queuetruth.cert.keys`: Ed25519 keys (`queuetruth cert keygen` / `cert pubkey`), signing and verification
- `queuetruth.cert.html.render(...)`: the HTML page (`certify --html`, `cert render`)
- `queuetruth.cert.tolerance`: the SPEC 7.2 cross-platform tolerance of re-executed outputs

What a certificate proves and what it does not: docs/how-to/certify-a-result.md. This package `__init__` imports
nothing, because `queuetruth.schemas.models` imports `queuetruth.cert.models` to register the documents.
"""
