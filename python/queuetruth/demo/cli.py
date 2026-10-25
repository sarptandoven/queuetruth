"""`queuetruth demo`: command line (routed by `queuetruth.cli.main` ahead of the clap grammar, see
`queuetruth.demo._dispatch`; `queuetruth demo --help` and `queuetruth help demo` print the flags).

Exit codes: 0 success, 2 any error (`QT-USAGE-001` bad flags or a download that was not confirmed, `QT-INPUT-001`
network or file errors, `QT-INPUT-002` an input that is not what the demo needs). With `--format json` exactly one
JSON document goes to stdout (`qt.demo_summary`, `qt.demo_plan` with `--dry-run`, or `qt.error`); progress goes to
stderr.

Downloads: the demo never downloads without consent. A cache miss on a remote source asks on a terminal, or needs
`--yes`; `--dry-run` shows the source, the estimated bytes and the cache path without doing anything.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Any

from queuetruth.errors import EXIT_ERROR, EXIT_OK, QtError

DESCRIPTION = """\
Fetch a small public order-level subset (the first minutes of a Nasdaq TotalView-ITCH or IEX DEEP+ day), place
simulated resting orders in the real queue, and show how often NautilusTrader's default fill model fills them
compared with the exact replay, by horizon, with base rates. Writes <out>/demo.html and <out>/demo.json.
"""


def parser() -> argparse.ArgumentParser:
    from queuetruth.demo import run as R
    from queuetruth.demo import sources as S

    p = argparse.ArgumentParser(
        prog="queuetruth demo", description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--source",
        default=S.DEFAULT_SOURCE,
        help=f"public day: {', '.join(S.SOURCES)} (aliases {', '.join(S.ALIASES)}; default {S.DEFAULT_SOURCE})",
    )
    p.add_argument("--symbols", help="comma-separated symbols (default: the source's four liquid names)")
    p.add_argument(
        "--minutes",
        type=int,
        default=R.DEFAULT_MINUTES,
        help=f"placement window length after 09:35 ET, minutes (default {R.DEFAULT_MINUTES})",
    )
    p.add_argument("--n", type=int, default=R.DEFAULT_N, help=f"simulated orders (default {R.DEFAULT_N})")
    p.add_argument(
        "--models",
        help=f"comma-separated zoo model ids, or all (default: {', '.join(R.DEMO_MODELS)})",
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--url", help="stream this http(s) URL instead (a day file or a pre-cut subset)")
    src.add_argument("--from-file", help="read this local day file or subset instead (gzip or raw)")
    p.add_argument(
        "--input-format", choices=["itch", "iex"], help="format of --url / --from-file (default: from the name)"
    )
    p.add_argument("--date", help="session date of --url / --from-file, YYYY-MM-DD (default: from the name)")
    p.add_argument("--out", default=R.DEFAULT_OUT, help=f"output directory (default {R.DEFAULT_OUT})")
    p.add_argument("--cache-dir", help="cache root (default: QT_CACHE_DIR or the user cache directory)")
    p.add_argument("--refresh", action="store_true", help="ignore a cached subset and fetch again")
    p.add_argument("--yes", "-y", action="store_true", help="download without asking")
    p.add_argument("--dry-run", action="store_true", help="show what would be downloaded and where, then stop")
    p.add_argument(
        "--max-download-mb",
        type=float,
        default=R.DEFAULT_MAX_DOWNLOAD_MB,
        help=f"stop if the cut is not reached after this many MB (default {R.DEFAULT_MAX_DOWNLOAD_MB:g})",
    )
    p.add_argument("--list-sources", action="store_true", help="list the known public days and exit")
    p.add_argument("--format", "--output-format", dest="output_format", choices=["human", "json"], default="human")
    p.add_argument("--threads", type=int, default=None, help="worker threads for the zoo (default min(4, cores))")
    p.add_argument("--seed", type=int, default=0, help="random seed (default 0)")
    p.add_argument("--quiet", action="store_true", help="no progress on stderr")
    p.add_argument("--verbose", action="store_true", help="also print every model's bias")
    p.add_argument("--log-level", choices=["error", "warn", "info", "debug"], default="warn", help=argparse.SUPPRESS)
    return p


class _Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # argparse exits 2 itself; route it through QT-USAGE-001
        raise _Usage(message)


def _options(ns: argparse.Namespace) -> Any:
    from queuetruth.demo.run import DemoOptions

    o = DemoOptions(
        source=ns.source,
        symbols=ns.symbols.split(",") if ns.symbols else None,
        minutes=ns.minutes,
        n=ns.n,
        seed=ns.seed,
        url=ns.url,
        from_file=ns.from_file,
        input_format=ns.input_format,
        date=ns.date,
        out=ns.out,
        cache_dir=ns.cache_dir,
        refresh=ns.refresh,
        yes=ns.yes,
        max_download_mb=ns.max_download_mb if ns.max_download_mb and ns.max_download_mb > 0 else None,
    )
    if ns.models:
        o.models = [m.strip() for m in ns.models.split(",") if m.strip()]
    if ns.threads is not None:
        if ns.threads < 1:
            raise QtError("QT-USAGE-001", "demo: --threads must be at least 1")
        o.threads = ns.threads
    return o


def _mb(n: int | None) -> str:
    return "unknown" if n is None else f"{n / 1e6:,.0f} MB"


def plan_text(pl: Any) -> str:
    src, w = pl.source, pl.window
    lines = [
        f"source: {src.title}, {src.session_date} ({src.location})",
        f"symbols: {' '.join(pl.symbols)}; orders placed {w.placement_start} to {w.placement_end} ET; "
        f"data cut {w.cut} ET",
        f"cache: {pl.cache_dir} ({'present' if pl.cache_hit else 'absent'})",
    ]
    if pl.download_needed:
        size = f" of a {pl.file_bytes / 1e9:,.2f} GB file" if pl.file_bytes else ""
        est_b = pl.estimated_download_bytes
        if est_b is None:
            est = "size not known in advance"
        elif pl.estimate_extrapolated:
            est = f"about {_mb(est_b)} (extrapolated past the last measured time)"
        else:
            est = f"about {_mb(est_b)}"
        lines.append(f"download: {est}{size}; the stream stops at the cut")
        cap = pl.max_download_bytes
        if est_b is not None and cap is not None and est_b > cap:
            lines.append(f"refused: more than --max-download-mb {cap / 1e6:,.0f}")
    else:
        lines.append("download: none")
    if src.attribution:
        lines.append(f"{src.attribution} {src.terms_url}")
    elif src.note:
        lines.append(src.note)
    if src.terms_url and not src.attribution:
        lines.append(f"source listing: {src.terms_url} (licensing: docs/data-and-licensing.md)")
    return "\n".join(lines)


def _ask(prompt: str) -> str:
    """The prompt on stderr (stdout may be redirected), one line from stdin ('' at end of input)."""
    print(prompt, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline()


def _confirm_factory(yes: bool, interactive: bool) -> Any:
    def confirm(pl: Any) -> bool:
        if yes:
            return True
        if not interactive:
            raise QtError(
                "QT-USAGE-001",
                f"demo: needs to download about {_mb(pl.estimated_download_bytes)} from {pl.source.location}"
                if pl.estimated_download_bytes is not None
                else f"demo: needs to download {pl.source.location} up to the data cut (size not known in advance)",
                "rerun with --yes to confirm the download, or --dry-run to see the plan",
            )
        print(plan_text(pl), file=sys.stderr)
        return _ask("Download now? [y/N] ").strip().lower() in ("y", "yes")

    return confirm


def _hz(ns: str) -> str:
    from queuetruth.eval.leaderboard import duration_text

    return duration_text(int(ns))


def _pp(x: float | None) -> str:
    if x is None or not math.isfinite(x):
        return "n/a"
    v = f"{x * 100:+.1f}"
    return "0.0" if float(v) == 0 else v


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def render(s: Any, verbose: bool = False) -> str:
    """The terminal answer: headline, table by horizon, the published reference, provenance, timings."""
    from queuetruth.cli import _table

    src, w, d = s.source, s.window, s.data
    cells = s.headline.cells
    keys = sorted(cells, key=int)
    scored = f"{s.n_leaderboard:,} scored" if s.n_leaderboard is not None else "scored count unknown"
    if s.n_excluded_hbt_312:
        scored += "*"
    lines = [
        f"queuetruth demo: {src.title}, {src.session_date}, {' '.join(d.symbols)}",
        f"{s.n_shadows:,} simulated resting limit orders placed {w.placement_start} to {w.placement_end} ET "
        f"({scored}), each followed for {w.h_max_s // 60} min in the exact replay of the real queue",
        "",
        s.headline.sentence,
        f"(model: {s.headline.verified_against})",
        "",
    ]
    rows = []
    for k in keys:
        c = cells[k]
        ci = f"[{_pp(c.ci_lo)}, {_pp(c.ci_hi)}]" if c.ci_lo is not None else "n/a"
        rows.append([_hz(k), _pct(c.exact_fill_rate), _pct(c.model_fill_rate), _pp(c.bias), ci])
    lines += _table(
        ["horizon", "exact fill rate", "NautilusTrader default", "overfill, pp", "95% CI"], rows, right={1, 2, 3, 4}
    )
    lines.append(f"(95% CI: cluster bootstrap over instrument x minute, {s.interval.clusters} clusters)")
    if s.reference is not None:
        ref = s.reference
        parts = [
            f"{_hz(k)} {_pp(v)} [{_pp(ref.ci_lo[k])}, {_pp(ref.ci_hi[k])}]"
            for k, v in sorted(ref.bias.items(), key=lambda kv: int(kv[0]))
        ]
        lines.append(f"published for this day, {ref.label}: {'; '.join(parts)} pp")
        note = reference_note(s)
        if note:
            lines.append(note)
    if s.n_excluded_hbt_312:
        lines.append(
            f"* {s.n_excluded_hbt_312:,} orders are left out of every model so that all are scored on the same "
            "orders (the zoo's common population): a faithful hftbacktest model stops on them (its issue #312)"
        )
    if verbose:
        lines += ["", "every zoo model, bias in pp:"]
        show = list(keys)
        vrows = [[m.model_id, *(_pp(m.cells[k].bias) if k in m.cells else "n/a" for k in show)] for m in s.models]
        lines += _table(["model", *(_hz(k) for k in show)], vrows, right=set(range(1, len(show) + 1)))
    lines.append("")
    if src.attribution:
        lines.append(f"{src.attribution} {src.terms_url}")
    elif src.note:
        lines.append(src.note)
    if d.cache_hit:
        got = "cached subset"
    elif s.data.remote:
        got = f"{d.bytes_downloaded / 1e6:,.1f} MB downloaded"
    else:
        got = f"{d.bytes_downloaded / 1e6:,.1f} MB read"
    lines.append(
        f"data: {got} ({d.subset_path}, {d.kept_messages:,} messages, {d.records:,} records, data quality "
        f"{d.data_quality}); cut at {w.cut} ET"
    )
    t = s.timings
    lines.append(
        f"time: {t.total_s:.1f} s (fetch {t.fetch_s:.1f}, ingest {t.ingest_s:.1f}, shadows {t.shadows_s:.1f}, "
        f"zoo {t.zoo_s:.1f}, answer {t.answer_s:.1f})"
    )
    lines.append(f"chart: {s.outputs.html}")
    lines.append(
        f"full report (with event-level traces; check the data licence before sharing it): "
        f"queuetruth report --in {s.outputs.zoo} --out report.html"
    )
    lines.append(f"run id {s.run_id}")
    return "\n".join(lines)


def reference_note(s: Any) -> str | None:
    """One line on why this window's bias can differ from the published full session's: the exact fill rates of
    both at the published horizons."""
    ref = s.reference
    if ref is None:
        return None
    keys = [k for k in sorted(ref.exact_fill_rate, key=int) if k in s.headline.cells]
    if not keys:
        return None
    here = " / ".join(_pct(s.headline.cells[k].exact_fill_rate) for k in keys)
    day = " / ".join(_pct(ref.exact_fill_rate[k]) for k in keys)
    hz = " / ".join(_hz(k) for k in keys)
    return (
        f"exact fill rate at {hz}: {here} in this window, {day} over the published session; where more orders "
        "fill under any model the overfill has less room to show"
    )


def _progress(quiet: bool) -> Any:
    if quiet:
        return None
    tty = sys.stderr.isatty()

    def show(d: dict[str, Any]) -> None:
        m = d.get("market_s")
        at = "-" if m is None else f"{int(m) // 3600:02d}:{int(m) % 3600 // 60:02d}:{int(m) % 60:02d}"
        rate = d["bytes"] / 1e6 / d["elapsed_s"] if d["elapsed_s"] > 0 else 0.0
        msg = f"  {d['bytes'] / 1e6:8.1f} MB ({rate:5.1f} MB/s), market time {at} ET, kept {d['kept']:,} messages"
        print(("\r" + msg) if tty else msg, end="" if tty else "\n", file=sys.stderr, flush=True)

    return show


def main(argv: list[str]) -> int:
    """`queuetruth demo` with `argv` (program and subcommand names removed)."""
    from queuetruth.cli import _wants_json

    p = parser()
    p.__class__ = _Parser
    json_out = _wants_json(argv)  # also for errors raised before the flags are parsed
    try:
        ns = p.parse_args(argv)
        json_out = ns.output_format == "json"
        from queuetruth.demo import run as R
        from queuetruth.demo import sources as S

        if ns.list_sources:
            from queuetruth.demo.summary import DemoSourceRow, DemoSources

            doc = DemoSources(
                default=S.DEFAULT_SOURCE,
                aliases=dict(S.ALIASES),
                sources=[
                    DemoSourceRow(
                        id=x.id,
                        title=x.title,
                        format=x.format,  # type: ignore[arg-type]
                        session_date=x.session_date,
                        url=x.url,
                        default_symbols=list(x.default_symbols),
                        attribution=x.attribution,
                        terms_url=x.terms_url,
                    )
                    for x in S.SOURCES.values()
                ],
            )
            if json_out:
                print(json.dumps(doc.model_dump(mode="json")))
            else:
                for x in doc.sources:
                    print(f"{x.id:20s} {x.title}, {x.session_date}, {' '.join(x.default_symbols)}")
            return EXIT_OK
        opts = _options(ns)
        if ns.dry_run:
            _, pl = R.plan(opts)
            print(json.dumps(pl.model_dump(mode="json")) if json_out else plan_text(pl))
            return EXIT_OK
        interactive = sys.stdin.isatty() and sys.stderr.isatty() and not json_out
        say = None if ns.quiet else (lambda s: print(f"demo: {s}", file=sys.stderr, flush=True))
        prog = _progress(ns.quiet)
        summary = R.run_demo(opts, confirm=_confirm_factory(ns.yes, interactive), say=say, fetch_progress=prog)
        if prog is not None and sys.stderr.isatty():
            print(file=sys.stderr)
        if json_out:
            print(json.dumps(summary.model_dump(mode="json"), ensure_ascii=False))
        else:
            print(render(summary, verbose=ns.verbose))
            if not ns.quiet:
                for w in summary.warnings:
                    print(f"warning: {w}", file=sys.stderr)
        return EXIT_OK
    except _Usage as e:
        err = QtError("QT-USAGE-001", f"demo: {e}", "queuetruth demo --help")
    except QtError as e:
        err = e
    except SystemExit as e:  # --help
        return int(e.code or 0) if isinstance(e.code, int) or e.code is None else EXIT_ERROR
    except KeyboardInterrupt:
        err = QtError("QT-USAGE-001", "interrupted")
    except Exception as e:  # every unexpected failure is QT-ENGINE-001 with exit code 2, as in queuetruth.cli
        err = QtError("QT-ENGINE-001", f"{type(e).__name__}: {e}")
    from queuetruth.cli import _emit_error

    return _emit_error(err, json_out)


__all__ = ["main", "parser", "plan_text", "reference_note", "render"]
