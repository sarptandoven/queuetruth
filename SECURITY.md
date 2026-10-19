# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub's private vulnerability reporting: open the repository's
**Security** tab and choose **Report a vulnerability**. Do not open a public issue, pull request or discussion for a
suspected vulnerability.

A useful report says which component is affected, the version or commit, the steps or input that trigger the
problem, and the impact you expect. Synthetic inputs are best; do not send real market data, orders or fills.

The maintainer aims to acknowledge a report within 7 days, agree on a fix and a disclosure date with you, and credit
you in the release notes unless you prefer otherwise.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | yes |
| older | no |

Fixes are released as a new patch version of the latest minor release.

## Scope

In scope:

- The self-hostable HTTP service (`service/`): authentication and API keys, tenant isolation (including the
  database's row-level security), idempotency, presigned upload and download URLs, job execution limits and
  zero-egress mode.
- The MCP server: its read-only default, the `--allow-submit` boundary and file access in local mode.
- Parsers and importers on untrusted input (ITCH, DBN, CSV, crypto captures, venue exports, FIX and QuickFIX logs):
  crashes, hangs or memory exhaustion from a crafted file.
- The release artifacts and the build and release workflows.

Not vulnerabilities on their own:

- Loading a learned model with `HazardModel.load(..., trusted=True)` from a directory someone else can write. Without
  `trusted=True` or a matching `sha256`, loading refuses to unpickle; see
  [The learned fill model](docs/concepts/learned-model.md).
- A self-hosted deployment that ignores the
  [production checklist](docs/how-to/self-host-the-service.md#production-checklist), for example one that exposes
  the database or uses the owner role for the api or the worker.
- Inaccurate fill or queue estimates. Those are correctness bugs; please use the bug report template.

The security model of the service, and the regression tests for each past finding, are described in the
[design specification](docs/design/spec.md) (section 8) and in `service/tests/security/`.
