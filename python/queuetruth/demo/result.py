"""`qt.demo(...)`: the demo as a typed Python function with the shared result shape (`queuetruth.results.Result`).

    import queuetruth as qt
    res = qt.demo(backtester="hftbacktest", minutes="5min", yes=True)
    print(res.summary)        # the answer sentence
    res.table()               # one row per horizon: exact and library fill rates, the bias and its 95% interval

`DemoResult.summary` is the headline sentence, `.doc` the `qt.demo_summary` document (also `<out>/demo.json`),
`.to_html()` the page `queuetruth demo` writes to `<out>/demo.html` and `.text()` what it prints.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar

from queuetruth import units
from queuetruth.errors import QtError
from queuetruth.results import Result

__all__ = ["DemoResult", "demo"]


class DemoResult(Result):
    """The answer of `qt.demo`: `.summary` (the headline sentence), `.summary_model` (the `DemoSummary`), `.doc`
    (its JSON document), `.table()` (one row per horizon), `.to_html()` (the demo page) and `.text()`."""

    title: ClassVar[str] = "queuetruth demo"

    def __init__(self, summary: Any) -> None:
        self.summary_model = summary
        self.doc = summary.model_dump(mode="json", by_alias=True)

    @property
    def summary(self) -> str:
        return str(self.summary_model.headline.sentence)

    @property
    def out(self) -> Path:
        """The output directory (`demo.html`, `demo.json`, the dataset, the shadows and the zoo run)."""
        return Path(self.summary_model.outputs.dir)

    def _cells(self) -> list[tuple[str, Any]]:
        cells = self.summary_model.headline.cells
        return [(k, cells[k]) for k in sorted(cells, key=int)]

    @property
    def interval(self) -> str | None:
        """The library's overfill at 1 s (else the first horizon) with its 95% cluster-bootstrap interval."""
        cells = self._cells()
        if not cells:
            return None
        k, c = next(((k, c) for k, c in cells if int(k) == 1_000_000_000), cells[0])
        if c.bias is None:
            return None
        ci = (
            f" (95% CI {c.ci_lo * 100:+.1f} to {c.ci_hi * 100:+.1f} pp, cluster bootstrap)"
            if c.ci_lo is not None and c.ci_hi is not None
            else ""
        )
        return f"Overfill at {units.format_duration(int(k))}: {c.bias * 100:+.1f} pp{ci}."

    def _rows(self) -> list[dict[str, Any]]:
        return [
            {
                "horizon": units.format_duration(int(k)),
                "exact_fill_rate": c.exact_fill_rate,
                "library_fill_rate": c.model_fill_rate,
                "bias_pp": None if c.bias is None else c.bias * 100,
                "ci_lo_pp": None if c.ci_lo is None else c.ci_lo * 100,
                "ci_hi_pp": None if c.ci_hi is None else c.ci_hi * 100,
                "n": c.n,
            }
            for k, c in self._cells()
        ]

    def to_html(self) -> str:
        """The page `queuetruth demo` writes to `<out>/demo.html`."""
        from queuetruth.demo.html import render_html

        return render_html(self.summary_model)

    def text(self) -> str:
        """What `queuetruth demo` prints."""
        from queuetruth.demo.cli import render

        return render(self.summary_model)


def _minutes(v: int | str) -> int:
    if isinstance(v, bool):
        raise QtError("QT-USAGE-001", f"demo: minutes must be a whole number of minutes, got {v!r}")
    if isinstance(v, int):
        ns = v * 60_000_000_000
    else:
        try:
            ns = units.parse_duration(v, what="minutes", bare_ns=False)
        except units.DurationError:
            try:
                ns = int(str(v).strip()) * 60_000_000_000
            except ValueError:
                raise QtError("QT-USAGE-001", f"demo: minutes: not a duration: {v!r}", "use e.g. 5 or 5min") from None
    if ns <= 0 or ns % 60_000_000_000:
        raise QtError("QT-USAGE-001", f"demo: minutes must be a positive whole number of minutes, got {v!r}")
    return ns // 60_000_000_000


def demo(
    backtester: str = "nautilus",
    *,
    symbols: Sequence[str] | str | None = None,
    minutes: int | str = 5,
    n: int = 3000,
    seed: int = 0,
    source: str | None = None,
    date: str | None = None,
    from_file: str | Path | None = None,
    url: str | None = None,
    out: str | Path = "qt-demo",
    cache_dir: str | Path | None = None,
    refresh: bool = False,
    yes: bool = False,
    confirm: Callable[[Any], bool] | None = None,
    max_download_mb: float | None = None,
    threads: int | None = None,
    say: Callable[[str], None] | None = None,
) -> DemoResult:
    """One public day, answered for `backtester` (`nautilus`, `hftbacktest` or `databento`): simulated resting limit
    orders replayed in the exact queue, against the library's default fill model (`queuetruth demo`).

    `minutes` is the placement window (`5`, `"5min"`); `symbols` a list or a comma-separated string. Nothing is
    downloaded unless `yes=True` or `confirm(plan)` returns True; `from_file` runs on a local ITCH or IEX DEEP+
    file instead. `out` receives `demo.html`, `demo.json` and the run's files."""
    from queuetruth.demo import sources as S
    from queuetruth.demo.run import DEFAULT_MAX_DOWNLOAD_MB, LIBRARIES, DemoOptions, run_demo

    if backtester not in LIBRARIES:
        raise QtError("QT-USAGE-001", f"demo: backtester must be one of {', '.join(LIBRARIES)}, got {backtester!r}")
    syms = [s.strip() for s in symbols.split(",") if s.strip()] if isinstance(symbols, str) else symbols
    opts = DemoOptions(
        source=source or S.DEFAULT_SOURCE,
        symbols=None if syms is None else list(syms),
        minutes=_minutes(minutes),
        n=int(n),
        seed=int(seed),
        url=url,
        from_file=None if from_file is None else str(from_file),
        date=date,
        out=str(out),
        cache_dir=None if cache_dir is None else str(cache_dir),
        refresh=refresh,
        yes=yes,
        max_download_mb=DEFAULT_MAX_DOWNLOAD_MB if max_download_mb is None else max_download_mb,
        backtester=backtester,
    )
    if threads is not None:
        opts.threads = int(threads)
    return DemoResult(run_demo(opts, confirm=confirm, say=say))
