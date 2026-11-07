#!/usr/bin/env python3
"""The upstream compatibility page: `compat.html` from the `qt.compat.result` documents `compat.py run` writes.

    python3 scripts/nightly/compat_site.py --results DIR --out SITE/compat.html [--atlas-link]

Reads every `compat-*.json` in DIR and writes one page: per library and version (newest first) the latest result
of its check, pass or fail per gate, the date and the link to the log; then every result, newest first. Styled with
the Fill-Realism Atlas site's CSS (`scripts/scale/atlas_site.py`) so the two pages read as one site. The page loads
nothing from the network and runs no script; the output is byte-identical for the same result files (no wall-clock
value: every date comes from a result). `--atlas-link` links back to the atlas index (`index.html` next to it).
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for _p in (HERE, HERE.parent / "scale"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import atlas_site as S  # noqa: E402  (stdlib only at import time)
from compat import SCHEMA_RESULT, valid_result  # noqa: E402
from qtnightly import envs as E  # noqa: E402

TITLE = "Upstream compatibility"
SUMMARY = "Whether the newest hftbacktest and NautilusTrader releases still match the exact replay of the queue."
REPO = "https://github.com/sarptandoven/queuetruth"
LIBRARY_NAMES = {"hftbacktest": "hftbacktest", "nautilus_trader": "NautilusTrader"}
CHECK_WORDS = {
    "hbt": "every compared order's first fill equals the faithful hbt zoo model's",
    "nt-pinned": "the pinned version's fills equal the faithful nt_* zoo models', queue model included",
    "nt-rc": "fills equal the faithful nt_* zoo models' (the 2.x queue model is tracked, not gated)",
}
EXTRA_CSS = """
table.text th, table.text td { text-align: left; }
td.pass { color: var(--under); } td.fail, td.error { color: var(--over); font-weight: 600; }
td.detail { white-space: normal; min-width: 18em; }
"""


def load_results(directory: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """(results oldest first, the file names skipped and why): unreadable files or other schemas never stop the
    page, they are listed on it."""
    docs, skipped = [], []
    for p in sorted(directory.glob("compat-*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            skipped.append(f"{p.name}: {type(e).__name__}")
            continue
        if not valid_result(d):
            skipped.append(f"{p.name}: not a valid {SCHEMA_RESULT} document")
            continue
        docs.append(d)
    docs.sort(key=lambda d: str(d.get("date", "")))
    return docs, skipped


def _e(x: Any) -> str:
    return html.escape(str(x))


def _link(url: Any, text: str) -> str:
    u = str(url or "")
    return f'<a href="{_e(u)}">{_e(text)}</a>' if u.startswith("https://") else ""


def _status(r: dict[str, Any]) -> str:
    s = str(r.get("status", "error"))
    return f'<td class="{_e(s) if s in ("pass", "fail", "error") else "error"}">{_e(s)}</td>'


def _gates(r: dict[str, Any]) -> str:
    gates = r.get("gates") or []
    passed = sum(1 for g in gates if g.get("ok"))
    return f"<td>{passed} / {len(gates)}</td>"


def _why(r: dict[str, Any]) -> str:
    """The failed gates with their detail, or the reason the check could not run."""
    if r.get("reason"):
        return _e(r["reason"])
    bad = [g for g in r.get("gates") or [] if not g.get("ok")]
    return "; ".join(f"{_e(g.get('name', ''))}: {_e(g.get('detail', ''))}" for g in bad)


def _vkey(v: str) -> tuple:
    k = E.version_key(v)
    return (1, k) if k is not None else (0, (v,))


def latest_by_version(docs: list[dict[str, Any]]) -> dict[str, list[tuple[dict[str, Any], dict[str, Any]]]]:
    """library -> [(result document, target row)] of each version's newest result, newest version first."""
    newest: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    for d in docs:  # oldest first, so a later date replaces an earlier one
        for r in d["targets"]:
            newest[(str(r.get("library")), str(r.get("version")))] = (d, r)
    out: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for (lib, _ver), pair in newest.items():
        out.setdefault(lib, []).append(pair)
    for lib in out:
        out[lib].sort(key=lambda p: _vkey(str(p[1].get("version"))), reverse=True)
    return dict(sorted(out.items(), key=lambda kv: (kv[0] not in LIBRARY_NAMES, kv[0])))


def _row(d: dict[str, Any], r: dict[str, Any], with_library: bool) -> str:
    cells = [f"<td>{_e(d.get('date', ''))}</td>"]
    if with_library:
        cells.append(f"<td>{_e(LIBRARY_NAMES.get(str(r.get('library')), r.get('library', '')))}</td>")
    cells += [
        f"<td><code>{_e(r.get('version', ''))}</code></td>",
        f"<td>{_e(', '.join(r.get('roles') or []))}</td>",
        f"<td><code>{_e(r.get('check', ''))}</code></td>",
        _status(r),
        _gates(r),
        f'<td class="detail">{_why(r)}</td>',
        f"<td>{_link(r.get('log_url'), 'log')} {_link(d.get('run_url'), 'run')}</td>",
    ]
    return "<tr>" + "".join(cells) + "</tr>"


def render(docs: list[dict[str, Any]], skipped: list[str] | None = None, atlas_link: bool = False) -> str:
    """The whole page (deterministic for the same documents)."""
    docs_url = f"{REPO}/blob/main/docs/how-to/nightly-checks.md"
    parts = [f"<h1>{TITLE}</h1>"]
    if atlas_link:
        parts.append('<p><a href="index.html">Back to the Fill-Realism Atlas</a></p>')
    parts.append(
        '<p class="muted">Every day the newest hftbacktest and NautilusTrader releases on PyPI (the newest of each '
        "major line, release candidates included) and the versions queuetruth pins are run on the same synthetic "
        "Nasdaq day as the faithful queuetruth models of their fill rules, order by order; a version passes when "
        f'every gate of its check holds (<a href="{docs_url}">what each check gates</a>). Synthetic data only: no '
        "market data is used or published.</p>"
    )
    if not docs:
        parts.append("<p>No results yet.</p>")
    else:
        last = docs[-1]
        rows = last["targets"]
        n_pass = sum(1 for r in rows if r.get("status") == "pass")
        parts.append(
            f'<p class="lead">On {_e(last.get("date", ""))}, {n_pass} of {len(rows)} version(s) passed'
            + (f" ({_link(last.get('run_url'), 'the run')})." if last.get("run_url") else ".")
            + "</p>"
        )
        head = (
            "<th>last checked</th><th>version</th><th>role</th><th>check</th><th>result</th><th>gates passed</th>"
            "<th>why not</th><th>links</th>"
        )
        for lib, pairs in latest_by_version(docs).items():
            name = LIBRARY_NAMES.get(lib, lib)
            checks = sorted({str(r.get("check")) for _, r in pairs})
            words = "; ".join(f"<code>{_e(c)}</code>: {_e(CHECK_WORDS.get(c, c))}" for c in checks)
            parts.append(
                f'<h2>{_e(name)}</h2><p class="muted">{words}.</p>'
                f'<div class="wrap"><table class="text"><thead><tr>{head}</tr></thead><tbody>'
                + "".join(_row(d, r, False) for d, r in pairs)
                + "</tbody></table></div>"
            )
        history = [(d, r) for d in reversed(docs) for r in d["targets"]]
        parts.append(
            "<h2>Every result</h2>"
            '<div class="wrap"><table class="text"><thead><tr><th>date</th><th>library</th>'
            + head.split("</th>", 1)[1]
            + "</tr></thead><tbody>"
            + "".join(_row(d, r, True) for d, r in history)
            + "</tbody></table></div>"
        )
        modes = sorted({str(d.get("mode", "")) for d in docs})
        parts.append(
            f'<p class="muted">{len(docs)} daily result(s) from {_e(docs[0].get("date", ""))} to '
            f"{_e(last.get('date', ''))}, nightly runner mode {_e(', '.join(modes))}. Each result is "
            f'<code>compat-&lt;date&gt;.json</code> on the <a href="{REPO}/releases/tag/compat-results">'
            "compat-results release</a>, with one log per version.</p>"
        )
    if skipped:
        parts.append('<p class="muted">Skipped: ' + "; ".join(_e(s) for s in skipped) + ".</p>")
    footer = (
        f'<p class="muted">Built by <a href="{REPO}">queuetruth</a> from <code>scripts/nightly/compat.py</code> '
        "results (<code>.github/workflows/compat.yml</code>).</p>"
    )
    return S._page(TITLE, "\n".join(parts), footer, SUMMARY, EXTRA_CSS)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", type=Path, required=True, help="directory of compat-*.json result documents")
    ap.add_argument("--out", type=Path, required=True, help="the page to write, e.g. SITE/compat.html")
    ap.add_argument("--atlas-link", action="store_true", help="link back to the atlas index next to the page")
    a = ap.parse_args(argv)
    docs, skipped = load_results(a.results) if a.results.is_dir() else ([], [])
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(render(docs, skipped, a.atlas_link), encoding="utf-8")
    print(json.dumps({"page": str(a.out), "results": len(docs), "skipped": skipped}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
