"""`queuetruth atlas`: the Fill-Realism Atlas answer for your library, tier or symbol, with the config to paste.

    queuetruth atlas [--library hftbacktest|nautilus|databento] [--symbol AAPL] [--tier T1_top100]
        [--horizon 1min] [--from DIR_OR_URL] [--yes] [--refresh] [--json]

Reads the atlas data API (`atlas/v1/`, `queuetruth.atlas`) from `--from DIR` or from the public site. Downloads follow
the rule of `queuetruth demo`: nothing is fetched until you agree (a prompt on a terminal, or `--yes`); the files go
to the user cache directory, are checked against the sha256 in the manifest, and are used offline afterwards
(`--refresh` gets the newest build). Prints the answer in plain words first, then the table, then the config to
paste; `--json` (or `--format json`) prints the `qt.atlas_answer` document. A Python-only group: `queuetruth.cli.main`
runs it before the clap grammar, which lists it (crates/cli/gen_python_commands.py).

Exit codes (SPEC 0.2): 0 success, 2 any error.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from typing import Any, NoReturn

from queuetruth.errors import EXIT_ERROR, EXIT_OK, QtError

#: Global flags of the main grammar that take a value (skipped when looking for the command word).
_VALUE_FLAGS = ("--format", "--output-format", "--log-level", "--threads", "--seed")


class _Parser(argparse.ArgumentParser):
    """argparse that raises `QT-USAGE-001` instead of exiting."""

    def error(self, message: str) -> NoReturn:
        raise QtError("QT-USAGE-001", f"{self.prog}: {message}", "queuetruth atlas --help")


def parser() -> argparse.ArgumentParser:
    """The argument parser of `queuetruth atlas`."""
    root = _Parser(prog="queuetruth", description="the Fill-Realism Atlas")
    root.add_argument("--format", "--output-format", dest="format", choices=["human", "json"], default="human")
    root.add_argument("--quiet", action="store_true")
    root.add_argument("--verbose", action="store_true")
    root.add_argument("--log-level", choices=["error", "warn", "info", "debug"], default="warn")
    root.add_argument("--threads", type=int, metavar="N")
    root.add_argument("--seed", type=int, metavar="N")
    sub = root.add_subparsers(dest="command", required=True, parser_class=_Parser)
    p = sub.add_parser(
        "atlas",
        help="how far your backtester's fill model is from the real queue, from the public atlas",
        description="The Fill-Realism Atlas answer for your library, liquidity tier or symbol: how far each setting "
        "fills from the exact replay of the real queue, with its 95% interval, and the closest setting's config to "
        "paste. Reads the atlas data API from a local directory or the public site (asks before it downloads).",
    )
    p.add_argument(
        "--library", choices=["hftbacktest", "nautilus", "databento"], help="your backtester (default: all three)"
    )
    p.add_argument("--symbol", metavar="SYMBOL", help="one symbol, e.g. AAPL (its own numbers, at 1s, 10s and 1min)")
    p.add_argument("--tier", metavar="TIER", help="liquidity tier, e.g. T1_top100 (default ALL, or the symbol's)")
    p.add_argument("--horizon", metavar="DURATION", help="fill horizon, e.g. 1s, 10s, 1min (default 1min)")
    p.add_argument(
        "--from",
        dest="source",
        metavar="DIR_OR_URL",
        help="a site or atlas/v1 directory, or the URL of a data API (default: the public atlas, cached)",
    )
    p.add_argument("--yes", "-y", action="store_true", help="download from the public atlas without asking")
    p.add_argument("--refresh", action="store_true", help="download the newest manifest even when a copy is cached")
    p.add_argument("--json", action="store_true", help="print the qt.atlas_answer document (same as --format json)")
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


def maybe_main(argv: list[str]) -> int | None:
    """The exit code when `argv` is `atlas` (or `help atlas`), else None (the main grammar handles it)."""
    word = command_word(argv)
    if word == "help":
        rest = [a for a in argv[argv.index("help") + 1 :] if not a.startswith("-")]
        return main(["atlas", "--help"]) if rest[:1] == ["atlas"] else None
    if word != "atlas":
        return None
    return main(argv)


def _ask(prompt: str) -> str:
    """The prompt on stderr (stdout may be redirected), one line from stdin ('' at end of input)."""
    print(prompt, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline()


def _confirm(interactive: bool) -> Any:
    def confirm(url: str) -> bool:
        if not interactive:
            raise QtError(
                "QT-USAGE-001",
                f"atlas: needs to download the atlas data API (a few MB) from {url.rsplit('/', 1)[0]}/",
                "rerun with --yes to confirm the download, or --from DIR for a local copy",
            )
        from queuetruth.demo.cache import user_cache_dir

        print(
            f"queuetruth atlas reads the public Fill-Realism Atlas: {url.rsplit('/', 1)[0]}/ (a few MB, checked "
            f"against its manifest, cached in {user_cache_dir() / 'atlas'}).",
            file=sys.stderr,
        )
        return _ask("Download now? [y/N] ").strip().lower() in ("y", "yes")

    return confirm


def main(argv: list[str]) -> int:
    """Runs `queuetruth atlas`; returns the exit code."""
    json_out = (
        "--json" in argv
        or any(a in ("--format=json", "--output-format=json") for a in argv)
        or any(a in ("--format", "--output-format") and b == "json" for a, b in itertools.pairwise(argv))
    )
    try:
        args = parser().parse_args(argv)
    except QtError as e:
        return _emit_error(e, json_out)
    except SystemExit as e:  # --help
        return int(e.code or 0)
    json_out = args.format == "json" or args.json
    from queuetruth.atlas._lookup import lookup

    interactive = sys.stdin.isatty() and sys.stderr.isatty() and not json_out
    try:
        res = lookup(
            library=args.library,
            symbol=args.symbol,
            tier=args.tier,
            horizon=args.horizon,
            source=args.source,
            yes=args.yes,
            refresh=args.refresh,
            confirm=_confirm(interactive),
        )
    except QtError as e:
        return _emit_error(e, json_out)
    except KeyboardInterrupt:
        return _emit_error(QtError("QT-USAGE-001", "interrupted"), json_out)
    except Exception as e:  # every unexpected failure is QT-ENGINE-001 with exit code 2
        return _emit_error(QtError("QT-ENGINE-001", f"{type(e).__name__}: {e}"), json_out)
    if json_out:
        print(json.dumps(res.doc, ensure_ascii=False, allow_nan=False))
    else:
        print(res.text())
    sys.stdout.flush()
    if not args.quiet:
        for w in res.doc.get("warnings") or []:
            print(f"warning: {w}", file=sys.stderr)
    return EXIT_OK


def _emit_error(e: QtError, json_out: bool) -> int:
    if json_out:
        print(json.dumps(e.to_document(), ensure_ascii=False, default=str))
    else:
        print(str(e), file=sys.stderr)
        if e.hint:
            print(f"hint: {e.hint}", file=sys.stderr)
    return EXIT_ERROR


__all__ = ["main", "maybe_main", "parser"]
