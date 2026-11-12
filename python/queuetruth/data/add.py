"""`queuetruth data add`: order-level files into one dataset, with format, venue and date inferred and the quality
checked.

    qt.data.add(["12302019.NASDAQ_ITCH50.gz", "01302020.NASDAQ_ITCH50.gz"], out="nasdaq")

Steps: every input is identified (`queuetruth.data.infer.detect`; given `format`, `venue` and `date` win); ITCH files
are ingested one per session date (their dates come from the names, a `manifest.json` next to them, or `date`), the
other formats in one `ingest` (their records carry their own dates); several ingests, and a dataset already at `out`,
are merged into one (`queuetruth.data.merge`). Everything is built in a staging directory next to `out` and swapped
in only when it succeeded, so a failed run leaves `out` as it was. Then the `dq` battery runs on the result and
writes `dq_report.json` and `dq_report.html` into it.

The result (`DataResult`) leads with one sentence (symbols, dates, records, data quality); `doc` is the `qt.data_add`
document (JSON-ready), `table()` one row per session date and symbol.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from queuetruth.data.infer import NEEDS_DATE, TITLES, Detected, detect
from queuetruth.data.merge import merge
from queuetruth.errors import QtError

SCHEMA = "qt.data_add"
CREATED_BY = "queuetruth data add"
EPOCH = dt.date(1970, 1, 1)

Say = Callable[[str], None]


@dataclass
class DataResult:
    """The answer of `data add` / `data fetch iex`: `summary` is the one-line answer, `doc` the full document, `rows`
    one row per session date and symbol in human units."""

    doc: dict[str, Any]
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """The answer in one plain sentence."""
        return str(self.doc["answer"])

    @property
    def path(self) -> Path:
        """The dataset directory."""
        return Path(self.doc["dataset"]["path"])

    def to_dict(self) -> dict[str, Any]:
        """The document (JSON-ready)."""
        return self.doc

    def table(self) -> Any:
        """One row per session date and symbol as a pandas DataFrame (venue-local times)."""
        try:
            import pandas as pd
        except ImportError as e:
            raise QtError("QT-CONFIG-001", "DataResult.table needs pandas", "pip install pandas") from e
        return pd.DataFrame(self.rows)

    def text(self) -> str:
        """The terminal summary: the answer first, then one line per session date, the checks and the inputs."""
        from queuetruth.data.render import text

        return text(self.doc)

    def __str__(self) -> str:
        return self.text()


def _date_of_days(days: int) -> str:
    return (EPOCH + dt.timedelta(days=int(days))).isoformat()


def _clock(ns: int | None, tz: str) -> str | None:
    if ns is None:
        return None
    t = dt.datetime.fromtimestamp(ns // 1_000_000_000, tz=ZoneInfo(tz))
    return t.strftime("%H:%M:%S")


def _is_dataset(p: Path) -> bool:
    return (p / "manifest.json").is_file()


def _plan(found: list[Detected], date: str | None, symbols: list[str] | None) -> list[dict[str, Any]]:
    """The ingest specs: one per ITCH session date, one for every other format (module docstring)."""
    keys = {(d.format, d.venue, d.source_venue) for d in found}
    if len(keys) > 1:
        kinds = "; ".join(f"{d.path.name}: {d.format} ({d.venue})" for d in found)
        raise QtError(
            "QT-USAGE-001",
            f"data add: the files are not one format and venue ({kinds}); one dataset holds one format",
            "add each format into its own --out",
        )
    fmt, venue, source_venue = keys.pop()
    base: dict[str, Any] = {"format": fmt, "venue": venue}
    if source_venue:
        base["source_venue"] = source_venue
    if symbols:
        base["symbols"] = list(symbols)
    if fmt not in NEEDS_DATE:
        spec = {**base, "inputs": [{"path": str(d.path)} for d in found]}
        if date:
            spec["session_date"] = date
        return [spec]
    by_date: dict[str, Detected] = {}
    for d in found:
        assert d.date is not None  # detect() asks for it otherwise
        if d.date in by_date:
            raise QtError(
                "QT-USAGE-001",
                f"data add: {by_date[d.date].path.name} and {d.path.name} are both {fmt} for {d.date}",
                "add one file per session date",
            )
        by_date[d.date] = d
    return [{**base, "inputs": [{"path": str(d.path)}], "session_date": day} for day, d in sorted(by_date.items())]


def _with_attribution(root: Path, attribution: list[dict[str, Any]]) -> None:
    p = root / "manifest.json"
    man = json.loads(p.read_text(encoding="utf-8"))
    have = man.get("attribution") or []
    for a in attribution:
        if a not in have:
            have.append(a)
    man["attribution"] = have
    p.write_text(json.dumps(man, indent=1) + "\n", encoding="utf-8")


def add(
    files: str | Path | Sequence[str | Path],
    out: str | Path,
    *,
    format: str | None = None,
    venue: str | None = None,
    date: str | None = None,
    symbols: Sequence[str] | None = None,
    attribution: list[dict[str, Any]] | None = None,
    say: Say | None = None,
) -> DataResult:
    """Ingests `files` into the dataset `out` (module docstring). An existing dataset at `out` keeps its days and
    gains the new ones (a day it already holds is `QT-INPUT-002`). `format`, `venue` and `date` override what is
    inferred (`date` is YYYY-MM-DD; for formats whose records carry dates it puts every record in that session);
    `symbols` keeps only those symbols; `attribution` entries are recorded in the dataset manifest."""
    note = say or (lambda _s: None)
    paths = [files] if isinstance(files, (str, Path)) else list(files)
    if not paths:
        raise QtError("QT-USAGE-001", "data add: needs at least one file", "queuetruth data add FILE --out DATASET")
    if date is not None:
        try:
            dt.date.fromisoformat(date)
        except ValueError:
            raise QtError("QT-USAGE-001", f"data add: invalid date {date!r}", "use YYYY-MM-DD") from None
    syms = [s.strip() for s in symbols or [] if s.strip()] or None
    found = [detect(p, format=format, venue=venue, date=date) for p in paths]
    specs = _plan(found, date, syms)
    out = Path(out)
    existing = _is_dataset(out)
    if out.exists() and not existing and (not out.is_dir() or any(out.iterdir())):
        raise QtError(
            "QT-USAGE-001",
            f"data add: --out {out} exists and is not a dataset",
            "choose a new directory, or an existing dataset to add days to",
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = out.parent / f".{out.name}.qt-add-{secrets.token_hex(4)}"
    stage.mkdir()
    try:
        import queuetruth as qt

        parts: list[Path] = []
        for i, spec in enumerate(specs):
            part = stage / f"part-{i}"
            names = ", ".join(Path(x["path"]).name for x in spec["inputs"])
            note(f"ingest {names} ({spec['format']}, venue {spec['venue']})")
            qt.ingest({**spec, "out": str(part)})
            parts.append(part)
        sources = ([out] if existing else []) + parts
        if len(sources) == 1:
            final = parts[0]
        else:
            note(f"merge {len(sources)} dataset(s)")
            final = stage / "merged"
            merge(sources, final, created_by=CREATED_BY)
        for leftover in ("dq_report.json", "dq_report.html"):
            (final / leftover).unlink(missing_ok=True)
        if attribution:
            _with_attribution(final, attribution)
        if existing:
            old = stage / "previous"
            out.rename(old)
            try:
                final.rename(out)
            except OSError:
                old.rename(out)
                raise
        else:
            if out.exists():
                out.rmdir()
            final.rename(out)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    note(f"check the data quality of {out}")
    from queuetruth.api import dq

    report = dq({"dataset": {"path": str(out)}, "out": str(out), "fail_on": "never"})
    added = sorted({s.get("session_date") for s in specs if s.get("session_date")})
    return describe(out, found, report, existing=existing, added_dates=added or None)


def describe(
    out: Path,
    found: list[Detected],
    report: Any,
    *,
    existing: bool = False,
    added_dates: list[str] | None = None,
) -> DataResult:
    """The `qt.data_add` document of the dataset at `out` after `add` (module docstring)."""
    import pyarrow.parquet as pq

    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    dq_counts = json.loads((out / "data_quality.json").read_text(encoding="utf-8"))
    inst = pq.read_table(out / "instruments.parquet", columns=["instrument_id", "session_date", "symbol", "session_tz"])
    names: dict[tuple[int, int], tuple[str, str]] = {}
    for iid, day, sym, tz in zip(
        inst["instrument_id"].to_pylist(),
        inst["session_date"].to_pylist(),
        inst["symbol"].to_pylist(),
        inst["session_tz"].to_pylist(),
        strict=True,
    ):
        day_n = day if isinstance(day, int) else (day - EPOCH).days
        names[(int(iid), int(day_n))] = (str(sym), str(tz or "UTC"))
    rows: list[dict[str, Any]] = []
    days: dict[int, dict[str, Any]] = {}
    for p in man["partitions"]:
        key = (int(p["instrument_id"]), int(p["session_date"]))
        sym, tz = names.get(key, (str(p["instrument_id"]), "UTC"))
        d = days.setdefault(key[1], {"tz": tz, "symbols": set(), "records": 0, "lo": None, "hi": None})
        d["symbols"].add(sym)
        d["records"] += int(p["rows"])
        lo, hi = p.get("ts_min"), p.get("ts_max")
        if lo is not None:
            d["lo"] = lo if d["lo"] is None else min(d["lo"], lo)
        if hi is not None:
            d["hi"] = hi if d["hi"] is None else max(d["hi"], hi)
        rows.append(
            {
                "date": _date_of_days(key[1]),
                "symbol": sym,
                "instrument_id": key[0],
                "records": int(p["rows"]),
                "first": _clock(lo, tz),
                "last": _clock(hi, tz),
                "timezone": tz,
            }
        )
    dates = [
        {
            "date": _date_of_days(day),
            "timezone": d["tz"],
            "first": _clock(d["lo"], d["tz"]),
            "last": _clock(d["hi"], d["tz"]),
            "symbols": len(d["symbols"]),
            "records": d["records"],
        }
        for day, d in sorted(days.items())
    ]
    symbols = sorted({r["symbol"] for r in rows})
    records = sum(d["records"] for d in dates)
    verdict = str(dq_counts.get("verdict", "unknown"))
    counts = report.counts.model_dump(mode="json", by_alias=True)
    not_passed = [
        {"id": c.id, "status": c.status, "summary": c.summary} for c in report.checks if c.status in ("warn", "fail")
    ]
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": 1,
        "answer": answer(out, symbols, [d["date"] for d in dates], records, verdict, report.status),
        "dataset": {
            "path": str(out),
            "dataset_id": man.get("dataset_id"),
            "kind": man.get("kind"),
            "format": man.get("source_format"),
            "format_title": TITLES.get(str(man.get("source_format")), man.get("source_format")),
            "venue": man.get("venue"),
            "source_venue": man.get("source_venue"),
            "rule_set": man.get("rule_set"),
        },
        "symbols": symbols,
        "dates": dates,
        "records": records,
        "partitions": len(man["partitions"]),
        "data_quality": {"verdict": verdict, "reasons": list(dq_counts.get("reasons") or [])},
        "checks": {"status": report.status, "counts": counts, "not_passed": not_passed},
        "inputs": [d.to_dict() for d in found],
        "added_to_existing": existing,
        "added_dates": added_dates,
        "attribution": man.get("attribution"),
        "files": {
            "manifest": str(out / "manifest.json"),
            "dq_report": str(out / "dq_report.json"),
            "dq_html": str(out / "dq_report.html"),
        },
        "next": f"queuetruth check BACKTEST --data {out}",
    }
    return DataResult(doc, rows)


def _symbols_text(symbols: list[str], limit: int = 4) -> str:
    if not symbols:
        return "no symbols"
    if len(symbols) <= limit:
        return ", ".join(symbols)
    return f"{', '.join(symbols[:limit])} and {len(symbols) - limit:,} more"


def _dates_text(dates: list[str]) -> str:
    if not dates:
        return "no session"
    if len(dates) == 1:
        return dates[0]
    if len(dates) == 2:
        return f"{dates[0]} and {dates[1]}"
    return f"{len(dates)} days, {dates[0]} to {dates[-1]}"


def answer(out: Path, symbols: list[str], dates: list[str], records: int, verdict: str, checks: str) -> str:
    """`nasdaq: AAPL, MSFT on 2019-12-30 and 2020-01-30, 46,838 records; data quality clean, checks pass.`"""
    return (
        f"{out}: {_symbols_text(symbols)} on {_dates_text(dates)}, {records:,} records; data quality {verdict}, "
        f"checks {checks}."
    )


__all__ = ["SCHEMA", "DataResult", "add", "answer", "describe"]
