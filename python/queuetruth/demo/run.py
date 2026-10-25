"""`queuetruth demo` end to end: real public order-level data to a fill-realism answer in about a minute.

Steps (each timed):
1. fetch: reuse the cached subset or stream the day file's prefix (`queuetruth.demo.subset`);
2. ingest: `qt.ingest` (format `itch` or `iex_deep_plus`) into `<out>/dataset`;
3. shadows: `qt.sample_shadows` with `n` never-cancel shadows (life = `h_max`) placed uniformly in the demo window
   (the sampler's `window_open_s` / `window_close_s` are set from the dataset's session hours so that the window
   is exactly `[start, end)`), the default placements and quantities of `shadows sample`;
4. zoo: `qt.run_zoo` with every default model in faithful mode at the default horizons (100 ms to 300 s);
5. answer: `queuetruth.demo.answer.cluster_intervals` and the `qt.demo_summary` document, `<out>/demo.json` and the
   chart `<out>/demo.html`.

`<out>` must be empty, absent, or a previous demo output: it holds the marker `.qt-demo` (written before anything
else, so a failed or interrupted run can be rerun into the same directory), a `demo.json` of schema
`qt.demo_summary`, or only the demo's own entries. Only the demo's own entries are replaced; `demo.html` and
`demo.json` are written last, each through a temporary file and a rename.

Consent: a remote cache miss downloads only when `DemoOptions.yes` is set or the `confirm` callback says yes; the
Python API never downloads by itself. A download whose estimate exceeds `max_download_mb` is refused before it starts.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import gzip
import json
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from queuetruth._version import __version__
from queuetruth.demo import answer as A
from queuetruth.demo import sources as S
from queuetruth.demo.cache import user_cache_dir
from queuetruth.demo.fetch import is_url, local_path
from queuetruth.demo.html import render_html
from queuetruth.demo.subset import cache_key, cached, fetch_subset
from queuetruth.demo.summary import (
    Cell,
    DataInfo,
    DemoPlan,
    DemoSummary,
    Headline,
    Interval,
    ModelRow,
    Outputs,
    Reference,
    SourceInfo,
    Timings,
    WindowInfo,
)
from queuetruth.errors import QtError
from queuetruth.schemas.runid import run_id

DEFAULT_MINUTES = 5
DEFAULT_N = 3000
DEFAULT_OUT = "qt-demo"
DEFAULT_MAX_DOWNLOAD_MB = 4000.0
OWNED = ("dataset", "shadows.parquet", "zoo", "demo.html", "demo.json")
MARKER = ".qt-demo"
HEADLINE_LABEL = "NautilusTrader's default fill model, FillModel(prob_fill_on_limit=1)"
#: docs/RESULTS.md, section 4 (NautilusTrader cross-check)
VERIFIED_AGAINST = (
    "queuetruth's faithful replica nt_prob_touch[p=1]; on the Nasdaq days 2019-12-30 and 2020-01-30 it fills exactly "
    "the same orders as nautilus_trader 1.231.0 (FillModel) and 2.0.0rc7 (DefaultFillModel) at 1 s, 10 s and 60 s "
    "(docs/RESULTS.md, section 4)"
)
#: The demo's default zoo: NautilusTrader's default and queue-position models, the hftbacktest model the published
#: results single out and its pessimistic risk-adverse model, Databento's queue estimator, and the two bounds. About
#: 40% less zoo time than the 16 models of `--models all` (measured on the default window).
DEMO_MODELS = (
    "nt_prob_touch[p=1]",
    "nt_queue_l2",
    "hbt_power3[n=2]",
    "hbt_risk_adverse",
    "databento_mc[k=-0.8;particles=100]",
    "front_of_queue",
    "trade_through_only",
)


@dataclass
class DemoOptions:
    """Inputs of `run_demo` (the CLI flags of the same names)."""

    source: str = S.DEFAULT_SOURCE
    symbols: list[str] | None = None
    minutes: int = DEFAULT_MINUTES
    n: int = DEFAULT_N
    seed: int = 0
    threads: int = field(default_factory=lambda: max(1, min(4, os.cpu_count() or 1)))
    url: str | None = None
    from_file: str | None = None
    input_format: str | None = None
    date: str | None = None
    out: str = DEFAULT_OUT
    cache_dir: str | None = None
    refresh: bool = False
    #: consent to download on a remote cache miss (else `run_demo` needs a `confirm` callback that says yes)
    yes: bool = False
    max_download_mb: float | None = DEFAULT_MAX_DOWNLOAD_MB
    bootstrap_b: int = 2000
    models: list[str] = field(default_factory=lambda: list(DEMO_MODELS))


@dataclass
class Resolved:
    """The options turned into a source, symbols, window and cache location."""

    source: S.Source
    symbols: list[str]
    window: S.DemoWindow
    cache_dir: Path
    cache_entry: Path
    remote: bool


def _symbols(raw: list[str] | None, default: tuple[str, ...]) -> list[str]:
    syms = [s.strip().upper() for s in (raw or list(default)) if s.strip()]
    if not syms:
        raise QtError("QT-USAGE-001", "demo: --symbols needs at least one symbol")
    bad = [s for s in syms if not (1 <= len(s) <= 8 and s.isascii() and s.replace(".", "").isalnum())]
    if bad:
        raise QtError("QT-USAGE-001", f"demo: invalid symbol(s) {', '.join(bad)}", "use exchange tickers like AAPL")
    if len(set(syms)) != len(syms):
        raise QtError("QT-USAGE-001", "demo: --symbols lists a symbol twice")
    return syms


def resolve(opts: DemoOptions) -> Resolved:
    """Source, symbols, window and cache entry of the options (no I/O besides a `stat` of `--from-file`)."""
    if opts.url and opts.from_file:
        raise QtError("QT-USAGE-001", "demo: --url and --from-file are mutually exclusive")
    if opts.n < 1:
        raise QtError("QT-USAGE-001", "demo: --n must be at least 1")
    if not opts.models:
        raise QtError("QT-USAGE-001", "demo: --models needs at least one model id (or all)")
    if opts.models != ["all"] and not any(A.find_model([{"model_id": m}], A.NT_DEFAULT) for m in opts.models):
        raise QtError(
            "QT-USAGE-001",
            f"demo: --models must include {A.NT_DEFAULT}, the model the demo's answer is about",
        )
    location = opts.url or opts.from_file
    if location:
        if opts.url and not is_url(opts.url):
            raise QtError(
                "QT-USAGE-001", f"demo: --url {opts.url!r} is not an http(s) URL", "use --from-file for files"
            )
        base = S.get_source(opts.source)
        side = S.read_sidecar(local_path(location)) if opts.from_file else None
        side_src = (side or {}).get("source") or {}
        name = S.file_name(location)
        fmt = (
            opts.input_format
            or S.infer_format(location)
            or side_src.get("format")
            or (_sniff_file(local_path(location)) if opts.from_file else None)
        )
        if fmt is None:
            raise QtError(
                "QT-USAGE-001",
                f"demo: cannot tell the format of {name!r} from its name",
                "pass --input-format itch or --input-format iex",
            )
        date = opts.date or S.infer_date(location) or side_src.get("session_date")
        if date is None:
            raise QtError(
                "QT-USAGE-001",
                f"demo: cannot infer the session date from {name!r}",
                "pass --date YYYY-MM-DD (the US/Eastern trading date of the data)",
            )
        if side and side_src.get("format") == fmt:
            default_syms = tuple(side.get("symbols") or ())
        else:
            default_syms = base.default_symbols if fmt == base.format else ()
        symbols = _symbols(opts.symbols, default_syms)
        source = S.custom_source(
            fmt, date, location if opts.url else str(local_path(location).resolve()), tuple(symbols)
        )
    else:
        if opts.input_format or opts.date:
            raise QtError("QT-USAGE-001", "demo: --input-format and --date apply to --url / --from-file only")
        source = S.get_source(opts.source)
        symbols = _symbols(opts.symbols, source.default_symbols)
    window = S.DemoWindow(source.session_date, opts.minutes)
    root = Path(opts.cache_dir).expanduser() if opts.cache_dir else user_cache_dir()
    extra = ""
    if opts.from_file:
        p = local_path(opts.from_file)
        try:
            st = p.stat()
        except OSError as e:
            raise QtError("QT-INPUT-001", f"{p}: {e.strerror or e}", "check the --from-file path") from None
        extra = f"{st.st_size}:{st.st_mtime_ns}"
    entry = root / "demo" / cache_key(source, symbols, window, extra)
    return Resolved(source, symbols, window, root, entry, is_url(source.url))


def _sniff_file(p: Path) -> str | None:
    """`S.sniff_format` of a local file's first bytes (inflated when it is gzip); None when unreadable."""
    try:
        with open(p, "rb") as f:
            magic = f.read(2)
            f.seek(0)
            if magic == b"\x1f\x8b":
                with gzip.GzipFile(fileobj=f) as g:
                    return S.sniff_format(g.read(16))
            return S.sniff_format(f.read(16))
    except (OSError, EOFError, gzip.BadGzipFile):
        return None


def _source_info(src: S.Source) -> SourceInfo:
    return SourceInfo(
        id=src.id,
        title=src.title,
        format=src.format,  # type: ignore[arg-type]
        session_date=src.session_date,
        location=src.url,
        attribution=src.attribution,
        terms_url=src.terms_url,
        note=src.note,
    )


def _window_info(w: S.DemoWindow) -> WindowInfo:
    return WindowInfo(
        placement_start=w.label(w.start_s),
        placement_end=w.label(w.end_s),
        cut=w.label(w.cut_s),
        minutes=w.minutes,
        h_max_s=w.h_max_s,
    )


def plan(opts: DemoOptions) -> tuple[Resolved, DemoPlan]:
    """What the run would do: whether the subset is cached and, if not, about how many bytes it downloads."""
    r = resolve(opts)
    hit = (not opts.refresh) and cached(r.cache_entry, r.source, r.symbols, r.window) is not None
    need = r.remote and not hit
    est = r.source.estimate_bytes(r.window.cut_s) if need else None
    doc = DemoPlan(
        source=_source_info(r.source),
        window=_window_info(r.window),
        symbols=r.symbols,
        cache_dir=str(r.cache_entry),
        cache_hit=hit,
        download_needed=need,
        estimated_download_bytes=est,
        estimate_extrapolated=est is not None and r.source.extrapolated(r.window.cut_s),
        max_download_bytes=_max_bytes(opts),
        file_bytes=r.source.listed_bytes,
        out=str(Path(opts.out)),
    )
    return r, doc


def _max_bytes(opts: DemoOptions) -> int | None:
    return int(opts.max_download_mb * 1e6) if opts.max_download_mb else None


def _is_demo_out(out: Path) -> bool:
    """`out` holds the marker, a `qt.demo_summary` document, or only the demo's own entries (module docstring)."""
    if (out / MARKER).is_file():
        return True
    try:
        doc = json.loads((out / "demo.json").read_text(encoding="utf-8"))
        if isinstance(doc, dict) and doc.get("schema") == "qt.demo_summary":
            return True
    except (OSError, ValueError):
        pass
    return all(p.name in OWNED for p in out.iterdir())


def prepare_out(out: Path, *, check_only: bool = False) -> None:
    """Creates `out` or clears the demo's own entries of a previous demo output (module docstring); `QT-USAGE-001`
    for a file or a non-empty directory that is not one. `check_only` raises the same errors and changes nothing."""
    if out.exists() and not out.is_dir():
        raise QtError("QT-USAGE-001", f"demo: --out {out} is a file", "choose a directory")
    if out.is_dir() and any(out.iterdir()) and not _is_demo_out(out):
        raise QtError(
            "QT-USAGE-001",
            f"demo: --out {out} is not empty and holds no previous demo output",
            "choose an empty or new directory",
        )
    if check_only:
        return
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text(
        "written by queuetruth demo: it replaces dataset/, shadows.parquet, zoo/, demo.html and demo.json here\n",
        encoding="utf-8",
    )
    for name in OWNED:
        p = out / name
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()


def _write_atomic(p: Path, text: str) -> None:
    tmp = p.with_name(f".{p.name}.part")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def _session_hours(dataset: Path) -> tuple[int, int]:
    t = pq.read_table(dataset / "instruments.parquet", columns=["open_s", "close_s"])
    opens = set(t.column("open_s").to_pylist())
    closes = set(t.column("close_s").to_pylist())
    if len(opens) != 1 or len(closes) != 1:
        raise QtError("QT-INPUT-002", f"{dataset}: instruments with different session hours ({opens}, {closes})")
    return int(opens.pop()), int(closes.pop())


def _cell(c: dict[str, Any]) -> Cell:
    return Cell(
        exact_fill_rate=c["exact_fill_rate"],
        model_fill_rate=c["model_fill_rate"],
        bias=c["value"],
        ci_lo=c["ci_lo"],
        ci_hi=c["ci_hi"],
        n=c["n"],
        ci_method=c["ci_method"],
    )


def headline_sentence(cells: dict[str, Cell], horizon_ns: int = 1_000_000_000) -> str:
    """'Within 1 s, NautilusTrader's default fill model fills 33.1% of these orders; the exact replay of the real
    queue fills 29.4%: 3.6 pp (1.12x) too many (95% CI +2.6 to +4.8 pp).'"""
    from queuetruth.eval.leaderboard import NEGLIGIBLE_BIAS, duration_text, pct

    c = cells.get(str(horizon_ns))
    when = duration_text(horizon_ns)
    if c is None or c.bias is None or c.exact_fill_rate is None or c.model_fill_rate is None:
        return f"No fill-rate bias within {when}: no order has an exact label at that horizon."
    ci = ""
    if c.ci_lo is not None and c.ci_hi is not None:
        ci = f" (95% CI {c.ci_lo * 100:+.1f} to {c.ci_hi * 100:+.1f} pp)"
    head = (
        f"Within {when}, NautilusTrader's default fill model fills {pct(c.model_fill_rate)}% of these orders; the "
        f"exact replay of the real queue fills {pct(c.exact_fill_rate)}%"
    )
    if abs(c.bias) < NEGLIGIBLE_BIAS:
        return f"{head}: the same{ci}."
    ratio = f" ({c.model_fill_rate / c.exact_fill_rate:.2f}x)" if c.exact_fill_rate > 0 else ""
    word = "too many" if c.bias > 0 else "too few"
    return f"{head}: {abs(c.bias) * 100:.1f} pp{ratio} {word}{ci}."


def run_demo(
    opts: DemoOptions,
    *,
    confirm: Callable[[DemoPlan], bool] | None = None,
    say: Callable[[str], None] | None = None,
    fetch_progress: Callable[[dict[str, Any]], None] | None = None,
) -> DemoSummary:
    """Runs the demo (module docstring). A remote cache miss downloads only with `opts.yes` or when `confirm(plan)`
    returns True (otherwise `QT-USAGE-001`); `say` receives one line per step."""
    t_all = time.perf_counter()
    note = say or (lambda _s: None)
    r, pl = plan(opts)
    out = Path(opts.out)
    prepare_out(out, check_only=True)  # refuse a busy directory before downloading anything
    warnings: list[str] = []

    # 1. fetch
    t0 = time.perf_counter()
    sub = None if opts.refresh else cached(r.cache_entry, r.source, r.symbols, r.window)
    if sub is None:
        if pl.download_needed:
            est, cap = pl.estimated_download_bytes, pl.max_download_bytes
            if est is not None and cap is not None and est > cap:
                raise QtError(
                    "QT-USAGE-001",
                    f"demo: reaching the data cut {pl.window.cut} ET needs about {est / 1e6:,.0f} MB, more than "
                    f"--max-download-mb {cap / 1e6:,.0f}",
                    "use a smaller --minutes, or raise --max-download-mb",
                )
            if not (opts.yes or (confirm is not None and confirm(pl))):
                hint = "pass --yes (DemoOptions.yes) to download without a prompt"
                raise QtError("QT-USAGE-001", "demo: download not confirmed", hint)
        note(f"fetch: {r.source.url}")
        sub = fetch_subset(
            r.source, r.symbols, r.window, r.cache_entry, max_download_bytes=_max_bytes(opts), progress=fetch_progress
        )
    else:
        note(f"fetch: cached subset {sub.path}")
    t_fetch = time.perf_counter() - t0
    prepare_out(out)
    man = sub.manifest
    flt = man["filter"]
    # an input whose content is a pinned subset (e.g. the demo's own subset fed back) is reported as that source
    src = r.source
    pin_id = man.get("pinned_source")
    if src.id == "custom" and pin_id in S.SOURCES:
        src = dataclasses.replace(S.SOURCES[pin_id], url=r.source.url)

    # 2. ingest
    t0 = time.perf_counter()
    ds = out / "dataset"
    note(f"ingest: {flt['kept_messages']:,} messages -> {ds}")
    import queuetruth as qt

    is_iex = src.format == "iex"
    ing = qt.ingest(
        {
            "format": "iex_deep_plus" if is_iex else "itch",
            "inputs": [{"path": str(sub.path)}],
            "venue": "iex" if is_iex else "nasdaq",
            "out": str(ds),
            "symbols": r.symbols,
            "session_date": src.session_date,
            "price_exp": -9,
            "tick_size": "0.01",
            "lot_size": "1",
        }
    )
    t_ingest = time.perf_counter() - t0
    dq = ing.data_quality
    if dq.verdict != "clean":
        warnings.append(f"data quality {dq.verdict}: {'; '.join(dq.reasons) or 'see dataset/data_quality.json'}")

    # 3. shadows
    t0 = time.perf_counter()
    open_s, close_s = _session_hours(ds)
    w = r.window
    if w.start_s < open_s or w.end_s > close_s:
        raise QtError("QT-INPUT-002", f"{ds}: session {open_s}-{close_s} s does not contain the demo window")
    shadows = out / "shadows.parquet"
    note(f"shadows: {opts.n:,} orders placed {w.label(w.start_s)} to {w.label(w.end_s)} ET")
    qt.sample_shadows(
        {
            "dataset": {"path": str(ds)},
            "out": str(shadows),
            "n": opts.n,
            "seed": opts.seed,
            "window_open_s": w.start_s - open_s,
            "window_close_s": close_s - w.end_s,
            "lifetime_ns": w.h_max_s * S.SEC_NS,
        }
    )
    t_shadows = time.perf_counter() - t0

    # 4. zoo
    t0 = time.perf_counter()
    zoo_dir = out / "zoo"
    what = "every default L2 model" if opts.models == ["all"] else f"{len(opts.models)} L2 models"
    note(f"zoo: {what} against the exact queue ({opts.threads} thread(s))")
    zoo = qt.run_zoo(
        {
            "dataset": {"path": str(ds)},
            "shadows": {"path": str(shadows)},
            "out": str(zoo_dir),
            "models": list(opts.models),
            "mode": "faithful",
            "h_max_ns": w.h_max_s * S.SEC_NS,
            "threads": opts.threads,
            "seed": opts.seed,
        }
    )
    t_zoo = time.perf_counter() - t0

    # 5. answer
    t0 = time.perf_counter()
    board = [e.model_dump(mode="json") for e in zoo.leaderboard]
    nt = A.find_model(board, A.NT_DEFAULT)
    if nt is None:
        raise QtError("QT-ENGINE-001", f"zoo leaderboard has no {A.NT_DEFAULT} row")
    cells, notes = A.cluster_intervals(
        zoo_dir, board, h_max_ns=w.h_max_s * S.SEC_NS, seed=opts.seed, b=opts.bootstrap_b
    )
    if notes["no_interval"]:
        reasons = sorted({x["reason"] for x in notes["no_interval"]})
        why = {
            "truncated_labels": f"{notes['shadows_with_truncated_labels']} shadow(s) have labels cut short by the end "
            "of data or a halt, so the exact fill rate is an Aalen-Johansen estimate, not a plain fraction",
            "mismatch": "the zoo's bias differs from the plain mean of the rows",
            "non_finite": "a model gave a non-finite fill probability",
        }
        warnings.append(
            f"no 95% interval for {len(notes['no_interval'])} model x horizon cell(s): "
            + "; ".join(why[r] for r in reasons)
        )
    models = [
        ModelRow(model_id=A.short_id(mid), cells={k: _cell(c) for k, c in cs.items()}) for mid, cs in cells.items()
    ]
    nt_cells = {k: _cell(c) for k, c in cells[str(nt["model_id"])].items()}
    head = Headline(
        model_id=str(nt["model_id"]),
        label=HEADLINE_LABEL,
        verified_against=VERIFIED_AGAINST,
        sentence=headline_sentence(nt_cells),
        cells=nt_cells,
    )
    ref_doc = S.REFERENCES.get(src.id)
    # identity (SPEC 7.3): what was computed and on which content; where the subset came from is provenance
    spec = {
        "demo": 1,
        "input": {"format": src.format, "session_date": src.session_date, "symbols": r.symbols},
        "window": {"start_s": w.start_s, "minutes": w.minutes, "h_max_s": w.h_max_s, "cut_s": w.cut_s},
        "n": opts.n,
        "seed": opts.seed,
        "models": sorted(zoo.models),
        "mode": "faithful",
        "interval": {"cluster_ns": A.CLUSTER_NS, "bootstrap_b": opts.bootstrap_b},
        "subset": {"path": str(sub.path)},
        "out": str(out),
        "threads": opts.threads,
    }
    content = flt["content_sha256"]
    rid = run_id(spec, __version__, lambda _ref: content)
    html_p, json_p = out / "demo.html", out / "demo.json"
    src_bytes = man["source"]
    summary = DemoSummary(
        run_id=rid,
        queuetruth_version=__version__,
        created=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        spec={k: v for k, v in spec.items() if k not in ("out", "threads")},
        source=_source_info(src),
        window=_window_info(w),
        data=DataInfo(
            symbols=r.symbols,
            subset_path=str(sub.path),
            subset_bytes=man["subset"]["bytes"],
            subset_sha256=man["subset"]["sha256"],
            content_sha256=flt["content_sha256"],
            kept_messages=flt["kept_messages"],
            cache_hit=sub.cache_hit,
            pinned=bool(man.get("pinned")),
            remote=r.remote,
            bytes_downloaded=sub.bytes_downloaded,
            file_bytes=r.source.listed_bytes or src_bytes.get("total_bytes"),
            resumptions=src_bytes.get("resumptions", 0) if not sub.cache_hit else 0,
            records=dq.records,
            data_quality=dq.verdict,
            data_quality_reasons=list(dq.reasons),
            dataset_id=getattr(ing.manifest, "dataset_id", None),
        ),
        n_shadows=zoo.n_shadows,
        n_leaderboard=zoo.n_leaderboard,
        n_excluded_hbt_312=zoo.n_excluded_hbt_312,
        zoo_run_id=zoo.run_id,
        headline=head,
        models=models,
        interval=Interval(
            method="cluster_bootstrap",
            cluster=notes["cluster"],
            clusters=notes["clusters"],
            bootstrap_b=notes["bootstrap_b"],
            note=(
                "95% percentile interval, clusters = instrument x minute of placement (the zoo's own interval "
                "resamples 30-minute buckets, of which a window of minutes has one or two)"
            ),
        ),
        reference=Reference.model_validate(ref_doc) if ref_doc else None,
        outputs=Outputs(
            dir=str(out),
            html=str(html_p),
            json=str(json_p),
            dataset=str(ds),
            shadows=str(shadows),
            zoo=str(zoo_dir),
        ),
        timings=Timings(fetch_s=0, ingest_s=0, shadows_s=0, zoo_s=0, answer_s=0, total_s=0),
        warnings=[*warnings, *[x for x in zoo.warnings if not x.startswith("hbt_312_abort: ")]],
    )
    t_answer = time.perf_counter() - t0
    summary.timings = Timings(
        fetch_s=round(t_fetch, 2),
        ingest_s=round(t_ingest, 2),
        shadows_s=round(t_shadows, 2),
        zoo_s=round(t_zoo, 2),
        answer_s=round(t_answer, 2),
        total_s=round(time.perf_counter() - t_all, 2),
    )
    _write_atomic(html_p, render_html(summary))
    _write_atomic(json_p, json.dumps(summary.model_dump(mode="json"), indent=2) + "\n")
    return summary


__all__ = [
    "DEFAULT_MINUTES",
    "DEFAULT_N",
    "DEMO_MODELS",
    "DemoOptions",
    "headline_sentence",
    "plan",
    "resolve",
    "run_demo",
]
