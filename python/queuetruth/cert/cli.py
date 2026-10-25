"""Command line of the certificate commands, dispatched by `queuetruth.cli.main` before the clap grammar:

    queuetruth certify RUN_DIR|LABEL=RUN_DIR... --out CERT.json [--claim TEXT]... [--key KEY.pem] [--html FILE]
        [--title TEXT] [--issuer TEXT] [--attach PATH]... [--commitment C.json --opening O.json]... [--base DIR]
    queuetruth verify-cert CERT.json [--rerun] [--only RUN]... [--base DIR] [--map OLD=NEW]... [--trust FPR]...
        [--pubkey PEM]... [--require-signature] [--work DIR] [--force-rerun] [--timeout S] [--out FILE]
        [--html FILE]
    queuetruth commit FILE... --out C.json [--opening O.json] [--name NAME]... [--label TEXT] [--key KEY.pem]
        [--force]
    queuetruth reveal C.json --opening O.json [FILE...] [--trust FPR]... [--pubkey PEM]...
    queuetruth cert keygen --out KEY.pem [--force]
    queuetruth cert pubkey KEY.pem
    queuetruth cert render CERT.json --out FILE.html [--verification V.json]
    queuetruth cert partial-opening O.json --item NAME... --out PARTIAL.json [--force]

Global flags as everywhere (`--format human|json`, `--quiet`, `--verbose`, `--log-level`, `--threads N`, `--seed
N`; `--threads` sets the worker threads of re-executed replay, zoo and calibration runs, whose outputs do not
depend on it, and `--seed` is accepted and ignored: a certified run's seed is in its spec). `queuetruth help
COMMAND` is `queuetruth COMMAND --help`. Exit codes (SPEC 0.2):
0 success, 1 a verification failed (`verify-cert`, `reveal`), 2 any error. With `--format json` exactly one JSON
document goes to stdout.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path
from typing import Any, NoReturn

from queuetruth.errors import EXIT_ERROR, EXIT_GATE, EXIT_OK, QtError

COMMANDS = ("certify", "verify-cert", "commit", "reveal", "cert")
#: Global flags of the main grammar that take a value (skipped when looking for the command word).
_VALUE_FLAGS = ("--format", "--output-format", "--log-level", "--threads", "--seed")


class _Parser(argparse.ArgumentParser):
    """argparse that raises `QT-USAGE-001` instead of exiting."""

    def error(self, message: str) -> NoReturn:
        words = self.prog.split()[1:]
        hint = (
            "queuetruth " + " ".join([*words, "--help"])
            if words
            else f"queuetruth COMMAND --help (COMMAND: {', '.join(COMMANDS)})"
        )
        raise QtError("QT-USAGE-001", f"{self.prog}: {message}", hint)


def _globals(p: argparse.ArgumentParser, suppress: bool) -> None:
    d = argparse.SUPPRESS if suppress else None
    p.add_argument("--format", "--output-format", dest="format", choices=["human", "json"], default=d or "human")
    p.add_argument("--quiet", action="store_true", default=d or False)
    p.add_argument("--verbose", action="store_true", default=d or False)
    p.add_argument("--log-level", choices=["error", "warn", "info", "debug"], default=d or "warn")
    p.add_argument("--threads", type=_positive_int, metavar="N", default=d)
    p.add_argument("--seed", type=int, metavar="N", default=d)


def _positive_int(text: str) -> int:
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}") from None
    if v < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}")
    return v


def parser() -> argparse.ArgumentParser:
    """The argument parser of the certificate commands."""
    root = _Parser(prog="queuetruth", description="certificates and commitments")
    _globals(root, suppress=False)
    sub = root.add_subparsers(dest="command", required=True, parser_class=_Parser)

    c = sub.add_parser("certify", help="certify run directories and claims")
    c.add_argument("runs", nargs="+", metavar="RUN_DIR", help="run directory, or LABEL=RUN_DIR")
    c.add_argument("--out", required=True, help="certificate JSON to write")
    c.add_argument("--claim", action="append", default=[], help="claim text with {RUN#/json/pointer} references")
    c.add_argument("--key", help="Ed25519 private key PEM to sign with (passphrase from QT_CERT_KEY_PASSPHRASE)")
    c.add_argument("--html", help="also write an HTML rendering")
    c.add_argument("--title")
    c.add_argument("--issuer", help="who issues the certificate (free text)")
    c.add_argument("--attach", action="append", default=[], help="extra file or directory to hash (no rerun)")
    c.add_argument("--commitment", action="append", default=[], help="qt.commitment to link (pair with --opening)")
    c.add_argument("--opening", action="append", default=[], help="qt.commitment_opening of the matching --commitment")
    c.add_argument("--base", help="directory the runs were launched from (default: current directory)")
    _globals(c, suppress=True)

    v = sub.add_parser("verify-cert", help="verify a certificate; --rerun re-executes every run")
    v.add_argument("cert")
    v.add_argument("--rerun", action="store_true", help="re-execute the runs and compare outputs bit for bit")
    v.add_argument("--only", action="append", default=[], metavar="RUN", help="verify only these runs")
    v.add_argument("--base", help="directory relative paths resolve against (default: current directory)")
    v.add_argument("--map", action="append", default=[], metavar="OLD=NEW", help="rewrite a path prefix")
    v.add_argument("--trust", action="append", default=[], metavar="FINGERPRINT", help="trusted key fingerprint")
    v.add_argument("--pubkey", action="append", default=[], metavar="PEM", help="trusted public key")
    v.add_argument("--require-signature", action="store_true", help="an unsigned certificate fails")
    v.add_argument(
        "--work",
        help="keep re-executed outputs in this new or empty directory (default: a removed temporary one)",
    )
    v.add_argument(
        "--force-rerun",
        action="store_true",
        help="re-execute even when the integrity, signature or key-pin check failed",
    )
    v.add_argument("--timeout", type=float, metavar="S", help="limit of each re-execution in seconds (default 6 h)")
    v.add_argument("--out", help="also write the qt.cert_verification document here")
    v.add_argument("--html", help="also write the certificate page with the verification results")
    _globals(v, suppress=True)

    m = sub.add_parser("commit", help="commit to files now, reveal later")
    m.add_argument("files", nargs="+", metavar="FILE")
    m.add_argument("--out", required=True, help="public qt.commitment to write")
    m.add_argument("--opening", help="private opening to write (default: <out>.opening.json, mode 0600)")
    m.add_argument("--name", action="append", default=[], help="committed name per file (default: file name)")
    m.add_argument("--label", help="public label (e.g. 'orders for 2026-10-26')")
    m.add_argument("--key", help="Ed25519 private key PEM to sign with")
    m.add_argument("--force", action="store_true", help="replace an existing commitment or opening file")
    _globals(m, suppress=True)

    r = sub.add_parser("reveal", help="check revealed files against a commitment")
    r.add_argument("commitment")
    r.add_argument("files", nargs="*", metavar="FILE")
    r.add_argument("--opening", required=True)
    r.add_argument("--trust", action="append", default=[], metavar="FINGERPRINT")
    r.add_argument("--pubkey", action="append", default=[], metavar="PEM")
    _globals(r, suppress=True)

    k = sub.add_parser("cert", help="signing keys and rendering")
    ksub = k.add_subparsers(dest="cert_command", required=True, parser_class=_Parser)
    kg = ksub.add_parser("keygen", help="generate an Ed25519 signing key")
    kg.add_argument("--out", required=True, help="private key PEM (public key written next to it as .pub.pem)")
    kg.add_argument("--force", action="store_true", help="replace existing key files")
    _globals(kg, suppress=True)
    kp = ksub.add_parser("pubkey", help="show the public key and fingerprint of a key file")
    kp.add_argument("key")
    _globals(kp, suppress=True)
    kr = ksub.add_parser("render", help="render a certificate as HTML")
    kr.add_argument("cert")
    kr.add_argument("--out", required=True)
    kr.add_argument("--verification", help="qt.cert_verification document to include")
    _globals(kr, suppress=True)
    ko = ksub.add_parser("partial-opening", help="write an opening that opens only some committed items")
    ko.add_argument("opening", help="the private qt.commitment_opening")
    ko.add_argument("--item", action="append", required=True, metavar="NAME", help="committed name to open")
    ko.add_argument("--out", required=True, help="partial qt.commitment_opening to write")
    ko.add_argument("--force", action="store_true", help="replace an existing file")
    _globals(ko, suppress=True)
    _globals(k, suppress=True)
    return root


def command_word(argv: list[str]) -> str | None:
    """The first positional word of `argv` (skipping global flags and their values)."""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _VALUE_FLAGS:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    return None


def _trusted(args: argparse.Namespace) -> set[str]:
    from queuetruth.cert import keys

    return {keys.normalize_fingerprint(t) for t in args.trust} | {
        keys.fingerprint(keys.load_public(Path(p))) for p in args.pubkey
    }


def _maps(items: list[str]) -> list[tuple[str, str]]:
    out = []
    for it in items:
        old, sep, new = it.partition("=")
        if not sep or not old or not new:
            raise QtError("QT-USAGE-001", f"--map {it!r}: expected OLD=NEW")
        out.append((old, new))
    return out


def _refuse_existing(*paths: Path) -> None:
    for p in paths:
        if p.exists():
            raise QtError(
                "QT-USAGE-001",
                f"{p} exists",
                "an opening holds the only copy of its nonces; pass --force to replace it, or write elsewhere",
            )


def _load_key(path: str | None) -> Any:
    if path is None:
        return None
    from queuetruth.cert import keys

    return keys.load_private(Path(path))[0]


def run(args: argparse.Namespace) -> tuple[dict[str, Any], bool, list[str]]:
    """Executes a parsed command: (document, verification failed, human lines)."""
    from queuetruth.cert import commit as qcommit
    from queuetruth.cert import html as qhtml
    from queuetruth.cert import keys
    from queuetruth.cert.util import write_json, write_text

    if args.command == "certify":
        from queuetruth.cert.certify import certify

        if len(args.commitment) != len(args.opening):
            raise QtError("QT-USAGE-001", "certify: give one --opening per --commitment, in the same order")
        out = Path(args.out)
        html = Path(args.html) if args.html else None
        cert = certify(
            args.runs,
            claims=args.claim,
            base=Path(args.base) if args.base else None,
            title=args.title,
            issuer=args.issuer,
            attach=args.attach,
            commitments=[(Path(c), Path(o)) for c, o in zip(args.commitment, args.opening, strict=True)],
            key=_load_key(args.key),
            out_files=[out] + ([html] if html else []),
            cert_name=out.name,
        )
        doc = cert.model_dump(mode="json")
        write_json(doc, out)
        if html is not None:
            write_text(qhtml.render(cert), html)
        sig = cert.integrity.signature
        who = f"signed by key {sig.key_fingerprint[:16]}" if sig is not None else "unsigned (sha256 digest only)"
        lines = [
            f"certificate {out}: {len(cert.runs)} run(s), {len(cert.inputs)} input(s), {len(cert.claims)} claim(s); "
            f"{who}; id {cert.certificate_id}"
        ]
        lines += [f"  {c.id}: {c.rendered}" for c in cert.claims]
        lines += [f"  run {r.label}: {r.kind} run_id {r.run_id} ({r.binding})" for r in cert.runs]
        if html is not None:
            lines.append(f"  html: {html}")
        return doc, False, lines
    if args.command == "verify-cert":
        from queuetruth.cert.models import Certificate
        from queuetruth.cert.verify import RERUN_TIMEOUT_S, verify_certificate

        if args.timeout is not None and not args.timeout > 0:
            raise QtError("QT-USAGE-001", f"--timeout {args.timeout:g}: must be positive seconds")

        ver = verify_certificate(
            Path(args.cert),
            rerun=args.rerun,
            only=args.only,
            base=Path(args.base) if args.base else None,
            mapping=_maps(args.map),
            trust=args.trust,
            pubkeys=[Path(p) for p in args.pubkey],
            require_signature=args.require_signature,
            work=Path(args.work) if args.work else None,
            force_rerun=args.force_rerun,
            threads=args.threads,
            timeout_s=args.timeout if args.timeout is not None else RERUN_TIMEOUT_S,
        )
        doc = ver.model_dump(mode="json")
        if args.out:
            write_json(doc, Path(args.out))
        if args.html:
            raw = json.loads(Path(args.cert).read_text(encoding="utf-8"))
            try:
                cert = Certificate.model_validate(raw)
            except ValueError:
                raise QtError("QT-INPUT-002", f"{args.cert}: not a valid certificate; no HTML written") from None
            write_text(qhtml.render(cert, ver), Path(args.html))
        c = ver.counts
        head = "VERIFIED" if ver.status == "pass" else "FAILED"
        mode = "with re-execution" if args.rerun else "files and claims (no re-execution: add --rerun)"
        signer = (
            f"signed by key {ver.certificate.key_fingerprint[:16]}"
            + (
                " (trusted)"
                if ver.certificate.key_trusted
                else " (not pinned)"
                if ver.certificate.key_trusted is None
                else " (NOT trusted)"
            )
            if ver.certificate.signed
            else "unsigned"
        )
        lines = [
            f"{head}: certificate {ver.certificate.certificate_id or '?'} ({signer}), {mode}: {c.pass_} passed, "
            f"{c.warn} warning(s), {c.fail} failed, {c.skip} skipped"
        ]
        for ch in ver.checks:
            if ch.status in ("fail", "warn") or args.verbose:
                lines.append(f"  {ch.status.upper():4} {ch.id}: {ch.summary}")
                if ch.status == "fail" or args.verbose:
                    lines += [f"         {d}" for d in ch.details]
        return doc, ver.status == "fail", lines
    if args.command == "commit":
        files = [Path(f) for f in args.files]
        if args.name and len(args.name) != len(files):
            raise QtError("QT-USAGE-001", f"commit: {len(args.name)} --name for {len(files)} file(s)")
        out = Path(args.out)
        opening_out = Path(args.opening) if args.opening else qcommit.default_opening_path(out)
        if not args.force:
            _refuse_existing(out, opening_out)
        c, opening = qcommit.commit(files, names=args.name or None, label=args.label, key=_load_key(args.key))
        qcommit.write_pair(c, opening, out, opening_out)
        signed = f", signed by key {c.signature.key_fingerprint[:16]}" if c.signature is not None else ""
        lines = [
            f"committed {c.n_items} file(s): root {c.root}{signed}",
            f"  public commitment: {out} (publish or hand over now)",
            f"  private opening:   {opening_out} (keep secret until reveal)",
        ]
        return c.model_dump(mode="json"), False, lines
    if args.command == "reveal":
        ver = qcommit.reveal(
            Path(args.commitment), Path(args.opening), [Path(f) for f in args.files], trusted=_trusted(args) or None
        )
        head = "REVEALED" if ver.status == "pass" else "FAILED"
        lines = [
            f"{head}: {sum(o.status == 'match' for o in ver.revealed)} of {ver.n_revealed} opened item(s) match "
            f"(commitment root {ver.commitment.root}, committed_at {qhtml.utc(ver.commitment.committed_at)} as "
            "asserted by the committer)"
        ]
        lines += [f"  {o.index} {o.name}: {o.status}" + (f" ({o.file})" if o.file else "") for o in ver.revealed]
        lines += [f"  unmatched file: {u}" for u in ver.unmatched_files]
        lines += [f"  {ch.status.upper():4} {ch.id}: {ch.summary}" for ch in ver.checks if ch.status == "fail"]
        return ver.model_dump(mode="json"), ver.status == "fail", lines
    if args.command == "cert":
        from queuetruth.cert.models import SigningKey

        if args.cert_command == "keygen":
            key, pub_path, encrypted = keys.generate(Path(args.out), force=args.force)
            doc = SigningKey(
                public_key=key.public_hex,
                key_fingerprint=key.fingerprint,
                private_key_path=args.out,
                public_key_path=str(pub_path),
                encrypted=encrypted,
            )
            lines = [
                f"Ed25519 key {key.fingerprint}",
                f"  private: {args.out} (mode 0600, "
                + ("encrypted)" if encrypted else "NOT encrypted: set QT_CERT_KEY_PASSPHRASE to encrypt it)"),
                f"  public:  {pub_path}",
                f"  verifiers pin it with: queuetruth verify-cert CERT --trust {key.fingerprint}",
            ]
            return doc.model_dump(mode="json"), False, lines
        if args.cert_command == "pubkey":
            p = Path(args.key)
            raw = keys.load_public(p)
            private = b"PRIVATE KEY" in p.read_bytes()
            doc = SigningKey(
                public_key=raw.hex(),
                key_fingerprint=keys.fingerprint(raw),
                private_key_path=str(p) if private else None,
                public_key_path=None if private else str(p),
                encrypted=(b"ENCRYPTED" in p.read_bytes()) if private else None,
            )
            return (
                doc.model_dump(mode="json"),
                False,
                [f"Ed25519 key {doc.key_fingerprint}", f"  public key {raw.hex()}"],
            )
        from queuetruth.cert.commit import load_doc

        if args.cert_command == "partial-opening":
            out = Path(args.out)
            if out.resolve() == Path(args.opening).resolve():
                raise QtError("QT-USAGE-001", "--out must not be the full opening it is cut from")
            if not args.force:
                _refuse_existing(out)
            part = qcommit.partial_opening(load_doc(Path(args.opening), qcommit.CommitmentOpening)[0], args.item)
            write_json(part.model_dump(mode="json"), out)
            lines = [f"wrote {out}: opens {len(part.items)} item(s): {', '.join(it.name for it in part.items)}"]
            return part.model_dump(mode="json"), False, lines
        from queuetruth.cert.models import Certificate, CertVerification

        cert, _ = load_doc(Path(args.cert), Certificate)
        ver = load_doc(Path(args.verification), CertVerification)[0] if args.verification else None
        text = qhtml.render(cert, ver)
        write_text(text, Path(args.out))
        from queuetruth.cert.hashing import sha256_bytes

        data = text.encode("utf-8")
        doc = {
            "schema": "qt.report_summary",
            "schema_version": 1,
            "inputs": [args.cert] + ([args.verification] if args.verification else []),
            "out": args.out,
            "sections": ["certificate"] + (["verification"] if ver is not None else []),
            "bytes": len(data),
            "sha256": sha256_bytes(data),
        }
        return doc, False, [f"wrote {args.out} ({len(data)} bytes)"]
    raise QtError("QT-USAGE-001", f"unknown command {args.command!r}")


def main(argv: list[str]) -> int:
    """Runs a certificate command; returns the exit code."""
    json_out = any(a in ("--format=json", "--output-format=json") for a in argv) or any(
        a in ("--format", "--output-format") and b == "json" for a, b in itertools.pairwise(argv)
    )
    try:
        args = parser().parse_args(argv)
    except QtError as e:
        return _emit_error(e, json_out)
    except SystemExit as e:  # --help
        return int(e.code or 0)
    json_out = args.format == "json"
    try:
        doc, failed, lines = run(args)
    except QtError as e:
        return _emit_error(e, json_out)
    except KeyboardInterrupt:
        return _emit_error(QtError("QT-USAGE-001", "interrupted"), json_out)
    except Exception as e:  # every unexpected failure is QT-ENGINE-001 with exit code 2
        return _emit_error(QtError("QT-ENGINE-001", f"{type(e).__name__}: {e}"), json_out)
    if json_out:
        print(json.dumps(doc, ensure_ascii=False, allow_nan=False))
    else:
        print("\n".join(lines))
    sys.stdout.flush()
    if not args.quiet:
        warns = doc.get("warnings") if isinstance(doc.get("warnings"), list) else []
        for w in warns:
            print(f"warning: {w}", file=sys.stderr)
        if failed:
            print("QT-GATE-001: a requested gate failed", file=sys.stderr)
    return EXIT_GATE if failed else EXIT_OK


def _emit_error(e: QtError, json_out: bool) -> int:
    if json_out:
        print(json.dumps(e.to_document(), ensure_ascii=False, default=str))
    else:
        print(str(e), file=sys.stderr)
        if e.hint:
            print(f"hint: {e.hint}", file=sys.stderr)
    return EXIT_ERROR if e.code != "QT-GATE-001" else EXIT_GATE


def maybe_main(argv: list[str]) -> int | None:
    """The exit code when `argv` is a certificate command (or `help` for one), else None (the main grammar handles
    it)."""
    word = command_word(argv)
    if word == "help":
        i = argv.index("help")
        rest = [a for a in argv[i + 1 :] if not a.startswith("-")]
        if rest and rest[0] in COMMANDS:
            return main([*rest, "--help"])
        return None
    if word not in COMMANDS:
        return None
    return main(argv)
