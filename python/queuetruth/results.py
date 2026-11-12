"""One shape for the result of every verb (plan section 4.2): `qt.check`, `qt.fit`, `qt.compare`,
`qt.atlas.lookup`, `qt.atlas.calibration`, `qt.data.add`, `qt.data.fetch_iex` and `qt.demo`.

Each result is a `Result` and has:

- `.summary`: the answer line, the same text the CLI prints first;
- `.table()`: the main table as a DataFrame (`queuetruth.frames.to_frame`, pandas first) in human units: symbols,
  decimal prices, venue-local times and horizons keyed by `1s`;
- `.to_html()`: the self-contained HTML page (the same page the CLI writes, where it writes one);
- `_repr_html_()`: a compact notebook view: the answer, the interval and the head of the main table;
- `.doc`: the JSON document, and `.to_dict()` the same;
- `.save(path)`: writes the document (`.json`, the default), the page (`.html`) or the table (`.csv`).

A verb's result class defines `doc`, keeps its own attributes, and overrides the hooks it needs: `_rows()` (the main
table as row dicts), `interval` (the interval behind the answer in words), `to_html()` and `text()`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from queuetruth.errors import QtError

__all__ = ["Result"]

#: Rows of the main table the notebook view shows.
REPR_ROWS = 10
#: Rows of the main table the generic HTML page shows.
HTML_ROWS = 500


def _esc(v: Any) -> str:
    import html

    return html.escape("" if v is None else str(v), quote=True)


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.6g}"
    if isinstance(v, dict | list):
        return json.dumps(v, ensure_ascii=False, default=str)
    return str(v)


def flat(row: dict[str, Any]) -> dict[str, Any]:
    """A row with nested objects spread into columns: `{"p": {"value": 0.5, "ci_lo": 0.4}}` becomes `p` and
    `p_ci_lo` (a nested `value` keeps the parent's name)."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, dict):
            for sub, x in flat(v).items():
                out[k if sub == "value" else f"{k}_{sub}"] = x
        else:
            out[k] = v
    return out


def html_table(rows: list[dict[str, Any]], limit: int) -> str:
    """A plain `<table>` of the first `limit` row dicts (columns are the union of their keys, in order)."""
    cols: list[str] = []
    for r in rows[:limit]:
        cols += [k for k in r if k not in cols]
    if not cols:
        return ""
    head = "".join(f"<th>{_esc(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{_esc(_cell(r.get(c)))}</td>" for c in cols) + "</tr>" for r in rows[:limit])
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


class Result:
    """The shared behaviour of every verb's result (module docstring). Subclasses set `doc`."""

    doc: dict[str, Any]
    #: The page title of the generic HTML page and the notebook view.
    title: ClassVar[str] = "queuetruth"

    @property
    def summary(self) -> str:
        """The answer in plain words (the first line the CLI prints)."""
        return str(self.doc.get("answer") or self.doc.get("summary") or "")

    @property
    def interval(self) -> str | None:
        """The interval behind the answer in words (`95% CI ...`), or None when the answer carries none."""
        return None

    def _rows(self) -> list[dict[str, Any]]:
        """The main table as row dicts in human units."""
        return []

    def _flat_rows(self) -> list[dict[str, Any]]:
        return [flat(r) for r in self._rows()]

    def table(self, backend: str | None = None) -> Any:
        """The main table as a DataFrame (pandas by default; `backend="polars"` or `QT_FRAMES_BACKEND`)."""
        from queuetruth import frames

        rows = self._flat_rows()
        if not rows:
            import pyarrow as pa

            return frames.to_frame(pa.table({}), backend)
        return frames.to_frame(rows, backend)

    def to_dict(self) -> dict[str, Any]:
        """The JSON document."""
        return self.doc

    def text(self) -> str:
        """The terminal view (the answer first)."""
        return self.summary

    def _html_sections(self) -> list[str]:
        """Extra sections of the generic page (HTML), after the main table."""
        return []

    def to_html(self) -> str:
        """A self-contained HTML page: the answer, the interval, the main table and the notes."""
        from queuetruth.report.page import Raw, section, shell, ul

        rows = self._flat_rows()
        body = [f'<header><h1>{_esc(self.title)}</h1><p class="lead" id="answer">{_esc(self.summary)}</p>']
        if self.interval:
            body.append(f"<p class='note'>{_esc(self.interval)}</p>")
        body.append("</header>")
        if rows:
            more = f" (first {HTML_ROWS} of {len(rows):,})" if len(rows) > HTML_ROWS else ""
            body.append(section("table", f"Table{more}", [Raw(html_table(rows, HTML_ROWS))]))
        body += self._html_sections()
        notes = self.doc.get("notes") or self.doc.get("warnings") or []
        if notes:
            body.append(section("notes", "Notes", [ul([str(n) for n in notes])]))
        return shell(self.title, "\n".join(str(b) for b in body), self.summary, skip_to="answer")

    def _repr_html_(self) -> str:
        """The compact notebook view: the answer, the interval and the head of the main table."""
        rows = self._flat_rows()
        parts = [f'<div class="qt-result"><p><strong>{_esc(self.summary)}</strong></p>']
        if self.interval:
            parts.append(f"<p>{_esc(self.interval)}</p>")
        if rows:
            parts.append(html_table(rows, REPR_ROWS))
            if len(rows) > REPR_ROWS:
                parts.append(f"<p><em>{REPR_ROWS} of {len(rows):,} rows; .table() has them all.</em></p>")
        parts.append("</div>")
        return "".join(parts)

    def save(self, path: str | Path) -> Path:
        """Writes the result to `path` by its suffix: `.html` the page, `.csv` the main table, anything else the JSON
        document. Returns the path."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        suffix = p.suffix.lower()
        if suffix in (".html", ".htm"):
            p.write_text(self.to_html(), encoding="utf-8")
        elif suffix == ".csv":
            import csv

            rows = self._flat_rows()
            cols: list[str] = []
            for r in rows:
                cols += [k for k in r if k not in cols]
            with p.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
                w.writeheader()
                w.writerows({k: _cell(v) for k, v in r.items()} for r in rows)
        else:
            try:
                text = json.dumps(self.doc, indent=2, ensure_ascii=False, allow_nan=False)
            except ValueError as e:
                raise QtError("QT-INPUT-002", f"save: the document has a non-finite number ({e})") from e
            p.write_text(text + "\n", encoding="utf-8")
        return p

    def __str__(self) -> str:
        return self.text()

    def __repr__(self) -> str:
        first = self.summary.splitlines()[0] if self.summary else ""
        return f"{type(self).__name__}({first!r})"
