"""`queuetruth ledger list | stats | export | verify | add`.

The Rust command grammar (`_core.cli_parse`) does not know `ledger`, so `queuetruth.cli.main` hands the arguments
here first (`main_if_ledger`); every other command line returns None and goes on to clap. Conventions as for the other
commands (SPEC 0.2): global `--format human|json` (also `--output-format`), `--quiet`, `--verbose`, `--log-level`
anywhere on the line; exactly one JSON document on stdout with `--format json`; exit 0 success, 2 any error (a
`qt.error` document with `--format json`), including `ledger verify` on a ledger that fails (`QT-LEDGER-001`).
"""

from __future__ import annotations

import argparse
import datetime
import itertools
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, NoReturn

from queuetruth.errors import EXIT_OK, QtError
from queuetruth.ledger import _capture as cap
from queuetruth.ledger import _query as q
from queuetruth.ledger.models import TRIAL_KINDS, LedgerFilter

_GLOBAL_VALUE = ("--log-level", "--output-format", "--format", "--seed", "--threads")
_GLOBAL_FLAG = ("--quiet", "--verbose")
DEFAULT_LIMIT = 20


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise QtError("QT-USAGE-001", f"{self.prog}: {message}", f"see `{self.prog} --help`")

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if status:
            raise QtError("QT-USAGE-001", (message or "").strip() or f"{self.prog}: invalid usage")
        if message:
            print(message, end="", file=sys.stderr)
        raise _Exit(status)


class _Exit(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def split_globals(argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    """(global options, the other arguments in order) of a command line."""
    g: dict[str, Any] = {"format": "human", "quiet": False, "verbose": False, "log_level": "warn"}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        name, eq, value = a.partition("=")
        if name in _GLOBAL_VALUE:
            if not eq:
                if i + 1 >= len(argv):
                    raise QtError("QT-USAGE-001", f"{name}: a value is required")
                value = argv[i + 1]
                i += 1
            key = "format" if name in ("--format", "--output-format") else name[2:].replace("-", "_")
            g[key] = value
        elif a in _GLOBAL_FLAG:
            g[a[2:]] = True
        else:
            rest.append(a)
        i += 1
    if g["format"] not in ("human", "json"):
        raise QtError("QT-USAGE-001", f"--format: {g['format']!r} is not human or json")
    return g, rest


def is_ledger_command(argv: list[str]) -> bool:
    """True when the first command word of `argv` is `ledger` (or the words are `help ledger`)."""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _GLOBAL_VALUE:
            i += 2
            continue
        if a.partition("=")[0] in _GLOBAL_VALUE or a in _GLOBAL_FLAG:
            i += 1
            continue
        return a == "ledger" or (a == "help" and argv[i + 1 : i + 2] == ["ledger"])
    return False


def _time_ns(text: str, what: str, *, code: str = "QT-USAGE-001") -> int:
    t = text.strip()
    if t.lstrip("-").isdigit() and t.isascii():
        return int(t)
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise QtError(code, f"{what}: {text!r} is not an ISO 8601 date or time (UTC when no offset) or ns") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    delta = dt - datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def parser() -> argparse.ArgumentParser:
    """The `queuetruth ledger` argument parser."""
    common = _Parser(add_help=False)
    common.add_argument(
        "--ledger",
        metavar="PATH",
        help="ledger directory or ledger.jsonl (default: $QT_LEDGER_DIR, else <project>/.queuetruth/ledger)",
    )
    flt = _Parser(add_help=False)
    flt.add_argument("--kind", action="append", choices=TRIAL_KINDS, help="record kind (repeatable)")
    flt.add_argument("--study", help="study name or glob")
    flt.add_argument("--name", help="variant or run name glob")
    flt.add_argument("--status", choices=("all", "ok", "error"), default="all", help="run status (default all)")
    flt.add_argument("--since", help="recorded at or after (ISO 8601 date or time, UTC when no offset; or ns)")
    flt.add_argument("--until", help="recorded before (same forms as --since)")
    flt.add_argument("--dataset", metavar="PREFIX", help="prefix of dataset_hash or of any input identity")
    flt.add_argument("--run-id", metavar="PREFIX", help="prefix of run_id (or of a variant's linked run ids)")
    flt.add_argument("--spec-hash", metavar="PREFIX", help="prefix of spec_hash")

    p = _Parser(prog="queuetruth ledger", description="trial ledger: every run and variant tried, hash-chained")
    sub = p.add_subparsers(dest="sub", metavar="COMMAND", required=True, parser_class=_Parser)
    sp = sub.add_parser("list", parents=[common, flt], help="list recorded trials")
    sp.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"newest N (default {DEFAULT_LIMIT}; 0 = all)")
    sub.add_parser("stats", parents=[common, flt], help="trial counts by kind and study")
    sp = sub.add_parser("export", parents=[common, flt], help="write the ledger as .jsonl, .json or .parquet")
    sp.add_argument("--out", required=True, metavar="PATH", help=".jsonl (whole chain), .json or .parquet")
    sp.add_argument("--selection", action="store_true", help="write the selection inputs (.json) instead")
    sp.add_argument("--series", choices=("returns", "scores"), default="returns", help="series kind (--selection)")
    sp.add_argument(
        "--allow-mixed", action="store_true", help="--selection: count trials across studies or kinds together"
    )
    sp = sub.add_parser("verify", parents=[common], help="check the hash chain; exit 2 if it is broken")
    sp.add_argument("--anchor", metavar="SEQ:HASH", help="a head noted earlier: the history up to it must be intact")
    sp = sub.add_parser("add", parents=[common], help="register a strategy variant you tried")
    sp.add_argument("--name", required=True, help="variant name (letters, digits, _ . : -)")
    sp.add_argument("--params", metavar="JSON", help="parameters as a JSON object, or @FILE")
    grp = sp.add_mutually_exclusive_group()
    grp.add_argument("--returns", metavar="FILE", help="per-period returns: .json, .csv/.txt or .parquet")
    grp.add_argument("--scores", metavar="FILE", help="per-unit scores: .json, .csv/.txt or .parquet")
    sp.add_argument("--column", metavar="NAME|N", help="value column of a multi-column file (default: the last)")
    sp.add_argument("--index-column", metavar="NAME|N", help="period-end column (ISO 8601 dates or times, UTC; or ns)")
    sp.add_argument("--lower-is-better", action="store_true", help="scores: lower is better (loss, Brier)")
    sp.add_argument("--periods-per-year", type=float, help="returns: periods per year (252 for daily)")
    sp.add_argument("--study", help="study (default $QT_LEDGER_STUDY or default)")
    sp.add_argument("--dataset", help="the data it was evaluated on: @PATH (identified by content) or a label")
    sp.add_argument("--code-version", help="your code version (commit, tag)")
    sp.add_argument("--run-id", action="append", default=[], metavar="RUN_ID", help="linked queuetruth run_id")
    sp.add_argument("--note", help="free text")
    return p


def _filter(a: argparse.Namespace) -> LedgerFilter:
    return q.as_filter(
        kinds=a.kind,
        study=a.study,
        name=a.name,
        status=a.status,
        since_ns=_time_ns(a.since, "--since") if a.since else None,
        until_ns=_time_ns(a.until, "--until") if a.until else None,
        dataset=a.dataset,
        run_id=a.run_id,
        spec_hash=a.spec_hash,
    )


_NUMERIC_START = re.compile(r"^[+-]?(?:[0-9]|\.[0-9])")


def _cell_number(text: str, where: str) -> float:
    try:
        v = float(text)
    except ValueError:
        raise QtError("QT-INPUT-002", f"{where}: {text[:40]!r} is not a number") from None
    return v


def _pick_column(names: list[str] | None, n_cols: int, spec: str | None, what: str, where: str) -> int:
    """Index of the column `spec` (a header name or a 0-based number) among `n_cols`; default: the last."""
    if spec is None:
        return n_cols - 1
    if names is not None and spec in names:
        return names.index(spec)
    if spec.isdigit() and int(spec) < n_cols:
        return int(spec)
    raise QtError("QT-USAGE-001", f"{what}: {spec!r} is not a column of {where} ({', '.join(names or [])})")


def read_series(
    path: str, *, column: str | None = None, index_column: str | None = None
) -> tuple[list[float], list[int] | None]:
    """(values, index or None) of a series file: a `.json` array (or `{"values": [...], "index": [...]}`); a
    `.parquet` table; or text, one row per line, columns separated by `,`, tab or `;`. `column` picks the value
    column of a multi-column file (header name or 0-based number; default the last) and `index_column` the column
    of period ends (ISO 8601 dates or times, UTC when no offset; or integer ns). A first text line is a header only
    when none of its cells starts like a number; any other cell that is not a number is `QT-INPUT-002`."""
    p = Path(path)
    if p.suffix.lower() == ".parquet":
        return _read_parquet(p, column, index_column)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise QtError("QT-INPUT-001", f"{p}: {e.strerror or e}") from None
    if p.suffix.lower() == ".json":
        if column is not None or index_column is not None:
            raise QtError("QT-USAGE-001", "--column / --index-column: apply to .csv, .txt and .parquet files")
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as e:
            raise QtError("QT-INPUT-002", f"{p}: invalid JSON: {e}") from None
        vals = doc.get("values") if isinstance(doc, dict) else doc
        ix = doc.get("index") if isinstance(doc, dict) else None
        if not isinstance(vals, list) or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            raise QtError("QT-INPUT-002", f"{p}: expected a JSON array of numbers (or {{'values': [...]}})")
        if ix is not None and not (isinstance(ix, list) and all(type(v) is int for v in ix)):
            raise QtError("QT-INPUT-002", f"{p}: index: expected an array of integer ns")
        return [float(v) for v in vals], ix
    rows = [(n, line.strip()) for n, line in enumerate(text.splitlines(), start=1) if line.strip()]
    if not rows:
        raise QtError("QT-INPUT-002", f"{p}: no values")
    sep = next((c for c in (",", "\t", ";") if c in rows[0][1]), None)
    cells = [(n, [c.strip() for c in line.split(sep)] if sep else [line]) for n, line in rows]
    n_cols = len(cells[0][1])
    names = None
    if not any(_NUMERIC_START.match(c) for c in cells[0][1]):
        names = cells[0][1]
        cells = cells[1:]
    vcol = _pick_column(names, n_cols, column, "--column", str(p))
    icol = _pick_column(names, n_cols, index_column, "--index-column", str(p)) if index_column is not None else None
    values: list[float] = []
    index: list[int] | None = [] if icol is not None else None
    for n, row in cells:
        if len(row) != n_cols:
            raise QtError("QT-INPUT-002", f"{p}: line {n}: {len(row)} column(s), expected {n_cols}")
        values.append(_cell_number(row[vcol], f"{p}: line {n}"))
        if index is not None and icol is not None:
            index.append(_time_ns(row[icol], f"{p}: line {n}", code="QT-INPUT-002"))
    if not values:
        raise QtError("QT-INPUT-002", f"{p}: no values")
    return values, index


def _read_parquet(p: Path, column: str | None, index_column: str | None) -> tuple[list[float], list[int] | None]:
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    try:
        t = pq.read_table(p)
    except (OSError, pa.ArrowException) as e:
        raise QtError("QT-INPUT-002", f"{p}: not a readable parquet file: {e}") from None
    vcol = _pick_column(t.column_names, t.num_columns, column, "--column", str(p))
    vals = t.column(vcol)
    if not (pa.types.is_floating(vals.type) or pa.types.is_integer(vals.type)) or vals.null_count:
        raise QtError("QT-INPUT-002", f"{p}: column {t.column_names[vcol]!r} is not numbers without nulls")
    index = None
    if index_column is not None:
        ic = t.column(_pick_column(t.column_names, t.num_columns, index_column, "--index-column", str(p)))
        if pa.types.is_timestamp(ic.type):
            ic = pc.cast(ic, pa.timestamp("ns", tz=ic.type.tz)).cast(pa.int64())
        elif pa.types.is_date(ic.type):
            ic = pc.cast(pc.cast(ic, pa.timestamp("ns")), pa.int64())
        if not pa.types.is_integer(ic.type) or ic.null_count:
            raise QtError("QT-INPUT-002", f"{p}: --index-column is not timestamps, dates or integer ns without nulls")
        index = [int(x) for x in ic.to_pylist()]
    return [float(x) for x in vals.to_pylist()], index


def _params(text: str | None) -> dict[str, Any] | None:
    if text is None:
        return None
    src = "--params"
    if text.startswith("@"):
        src = text[1:]
        try:
            text = Path(src).read_text(encoding="utf-8")
        except OSError as e:
            raise QtError("QT-INPUT-001", f"{src}: {e.strerror or e}") from None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise QtError("QT-USAGE-001", f"{src}: invalid JSON: {e}") from None
    if not isinstance(doc, dict):
        raise QtError("QT-USAGE-001", f"{src}: expected a JSON object")
    return doc


def run(argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Runs `ledger <argv>`: (the command's JSON document, warning lines for stderr)."""
    a = parser().parse_args(argv)
    led = cap.as_ledger(a.ledger)
    warn: list[str] = []
    if a.sub == "list":
        if a.limit < 0:
            raise QtError("QT-USAGE-001", "--limit: must be >= 0")
        return q.list_document(_filter(a), limit=a.limit, ledger=led).model_dump(mode="json"), warn
    if a.sub == "stats":
        return q.stats(_filter(a), ledger=led).model_dump(mode="json"), warn
    if a.sub == "export":
        if a.series != "returns" and not a.selection:
            raise QtError("QT-USAGE-001", "--series: applies with --selection")
        if a.allow_mixed and not a.selection:
            raise QtError("QT-USAGE-001", "--allow-mixed: applies with --selection")
        doc = q.export(a.out, _filter(a), selection=a.selection, series=a.series, allow_mixed=a.allow_mixed, ledger=led)
        return doc.model_dump(mode="json"), warn
    if a.sub == "verify":
        v = led.verify(a.anchor).model_dump(mode="json")
        if v["uncommitted_tail_bytes"]:
            warn.append(
                f"{v['uncommitted_tail_bytes']} byte(s) after the last record: an append that was cut off; the next "
                "append moves them to torn/"
            )
        if not v["ok"]:
            first = v["problems"][0] if v["problems"] else {"line": 0, "reason": "?", "message": ""}
            raise QtError(
                "QT-LEDGER-001",
                f"{led.path}: {v['n_problems']} integrity problem(s); first at line {first['line']}: "
                f"{first['reason']}: {first['message']}",
                "the ledger is not trusted until it verifies; restore it from a copy, or start a new ledger",
                details={"verification": v},
            )
        return v, warn
    # add
    if a.lower_is_better and a.scores is None:
        raise QtError("QT-USAGE-001", "--lower-is-better: applies to --scores")
    if a.periods_per_year is not None and a.returns is None:
        raise QtError("QT-USAGE-001", "--periods-per-year: applies to --returns")
    src = a.returns or a.scores
    if src is None and (a.column is not None or a.index_column is not None):
        raise QtError("QT-USAGE-001", "--column / --index-column: apply to --returns or --scores")
    values, index = read_series(src, column=a.column, index_column=a.index_column) if src else (None, None)
    if cap.switch() == "off":
        raise QtError("QT-USAGE-001", "QT_LEDGER=off: recording is disabled", "unset QT_LEDGER to register variants")
    rec = cap.register_variant(
        a.name,
        _params(a.params),
        returns=values if a.returns else None,
        scores=values if a.scores else None,
        index=index,
        higher_is_better=not a.lower_is_better,
        periods_per_year=a.periods_per_year,
        study=a.study,
        dataset=a.dataset,
        code_version=a.code_version,
        run_ids=a.run_id,
        note=a.note,
        ledger=led,
    )
    assert rec is not None
    return rec.model_dump(mode="json"), warn


# ---- human output -----------------------------------------------------------------------------------------------


def _utc(ns: int | None) -> str:
    if ns is None:
        return "-"
    dt = datetime.datetime.fromtimestamp(ns // 1_000_000_000, tz=datetime.UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _head(h: dict[str, Any] | None) -> str:
    return f"{h['seq']}:{h['hash']}" if h else "-"


def _metric_text(r: dict[str, Any]) -> str:
    if r.get("status") == "error" and r.get("error"):
        return f"{r['error']['code']}: {r['error']['message'][:60]}"
    parts = []
    s = r.get("series")
    if s:
        parts.append(f"{s['kind']} n={s['n_obs']}")
    for k, v in list((r.get("metrics") or {}).items())[:3]:
        text = f"{v:.6g}" if isinstance(v, float) else str(v)
        parts.append(f"{k}={text if len(text) <= 40 else text[:37] + '...'}")
    return " ".join(parts)


def _table(rows: list[list[str]]) -> list[str]:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows]


def render(sub: str, doc: dict[str, Any]) -> str:
    """Answer-first terminal rendering of a ledger document."""
    if sub == "list":
        recs = doc["records"]
        lines = [f"{len(recs)} of {doc['n_matching']} matching record(s) in {doc['ledger']}"]
        if doc["n_conflicts"]:
            lines.append(f"{doc['n_conflicts']} trial(s) whose reruns gave different series")
        if recs:
            rows = [["seq", "recorded (UTC)", "kind", "study", "status", "trial", "name", "result"]]
            for r in recs:
                rows.append(
                    [
                        str(r["seq"]),
                        _utc(r["recorded_at_ns"]),
                        r["kind"],
                        r.get("study") or "-",
                        r["status"],
                        r.get("trial_id") or "-",
                        r.get("name") or "-",
                        _metric_text(r),
                    ]
                )
            lines += _table(rows)
        lines.append(f"head: {_head(doc['head'])}")
        return "\n".join(lines)
    if sub == "stats":
        lines = [
            f"{doc['n_trials']} trial(s) ({doc['n_configs']} configuration(s), {doc['n_records']} record(s), "
            f"{doc['n_errors']} failed run(s), {doc['n_with_series']} with a series) in {doc['ledger']}"
        ]
        if doc["n_conflicts"] or doc["n_linked"]:
            lines.append(
                f"{doc['n_conflicts']} trial(s) whose reruns gave different series (each series counted), "
                f"{doc['n_linked']} run(s) counted with the variant that links them"
            )
        for title, key in (("by kind", "by_kind"), ("by study", "by_study")):
            if doc[key]:
                lines.append(f"{title}:")
                rows = [["", "records", "trials", "configs", "errors"]]
                rows += [
                    [k, str(v["records"]), str(v["trials"]), str(v["configs"]), str(v["errors"])]
                    for k, v in doc[key].items()
                ]
                lines += ["  " + x for x in _table(rows)]
        if doc["first_recorded_at_ns"] is not None:
            lines.append(f"recorded: {_utc(doc['first_recorded_at_ns'])} to {_utc(doc['last_recorded_at_ns'])} UTC")
        lines.append(f"head: {_head(doc['head'])}")
        return "\n".join(lines)
    if sub == "export":
        return (
            f"wrote {doc['n_records']} {'trial(s)' if doc['format'] == 'selection' else 'record(s)'} to {doc['out']} "
            f"({doc['format']}, {doc['bytes']} bytes, sha256 {doc['sha256']})\nhead: {_head(doc['head'])}"
        )
    if sub == "verify":
        lines = [f"ledger OK: {doc['n_records']} record(s) with genesis, hash chain intact ({doc['ledger']})"]
        if doc.get("anchor"):
            lines.append(f"anchor {doc['anchor']}: present and unchanged")
        lines.append(
            f"head: {_head(doc['head'])} (note it outside the ledger; `ledger verify --anchor <head>` later proves "
            "the history up to it is unchanged)"
        )
        return "\n".join(lines)
    return f"recorded variant {doc['name']} as trial {doc['trial_id']} (seq {doc['seq']}, study {doc['study']})" + (
        f", {doc['series']['kind']} n={doc['series']['n_obs']}" if doc.get("series") else ""
    )


def main_if_ledger(argv: list[str]) -> int | None:
    """Runs a `ledger` command line and returns its exit code; None when `argv` is another command."""
    if not is_ledger_command(argv):
        return None
    json_out = any(a in ("--format=json", "--output-format=json") for a in argv) or any(
        a in ("--format", "--output-format") and b == "json" for a, b in itertools.pairwise(argv)
    )
    try:
        g, rest = split_globals(argv)
        json_out = g["format"] == "json"
        level = getattr(logging, str(g["log_level"]).upper().replace("WARN", "WARNING"), logging.WARNING)
        logging.basicConfig(
            stream=sys.stderr, level=logging.ERROR if g["quiet"] else level, format="%(levelname)s %(message)s"
        )
        if rest[:1] == ["help"]:  # `queuetruth help ledger [COMMAND]` = `queuetruth ledger [COMMAND] --help`
            rest = ["ledger", *rest[2:], "--help"]
        sub = rest[1] if len(rest) > 1 else ""
        doc, warn = run(rest[1:])
    except _Exit as x:
        return x.code
    except QtError as e:
        return _emit_error(e, json_out)
    except Exception as e:  # every unexpected failure is QT-ENGINE-001 with exit code 2
        return _emit_error(QtError("QT-ENGINE-001", f"{type(e).__name__}: {e}"), json_out)
    if json_out:
        print(json.dumps(doc, ensure_ascii=False, allow_nan=False))
    else:
        print(render(sub, doc))
    if not g["quiet"]:
        sys.stdout.flush()
        for w in warn:
            print(f"warning: {w}", file=sys.stderr)
    return EXIT_OK


def _emit_error(e: QtError, json_out: bool) -> int:
    if json_out:
        print(json.dumps(e.to_document(), ensure_ascii=False, default=str))
    else:
        print(str(e), file=sys.stderr)
        if e.hint:
            print(f"hint: {e.hint}", file=sys.stderr)
    return e.exit_code
