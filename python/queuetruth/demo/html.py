"""`<out>/demo.html`: one self-contained page (no script, no external request) with the demo's answer.

Two charts share the horizons on their x axis: the fill rate of the exact replay next to NautilusTrader's default
fill model (grouped bars, two series, legend), and the model's bias in percentage points with its 95% interval
(dot and whisker; the published full-session value as a hollow marker where the source has one). A table repeats
every number, then the other zoo models' biases, the run's warnings, then provenance (source, attribution, subset
sha256, run id). A local input file appears by its name only, so the page can be shared without the local path.
Light and dark themes follow the viewer's setting. Every mark has a `<title>` tooltip.
"""

from __future__ import annotations

import html
import math

from queuetruth.demo.fetch import is_url
from queuetruth.demo.sources import file_name
from queuetruth.demo.summary import Cell, DemoSummary
from queuetruth.eval.leaderboard import duration_text

W, H = 640, 260
PAD_L, PAD_R, PAD_T, PAD_B = 56, 16, 16, 40

CSS = """
.qt-demo { color-scheme: light;
  --surface: #fcfcfb; --text: #0b0b0b; --text-2: #52514e; --grid: #e2e1dc; --axis: #8a8984;
  --exact: #2a78d6; --model: #eb6834; --ref: #52514e; }
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .qt-demo { color-scheme: dark;
    --surface: #1a1a19; --text: #f0efec; --text-2: #c3c2b7; --grid: #34332f; --axis: #77766f;
    --exact: #3987e5; --model: #d95926; --ref: #c3c2b7; }
}
:root[data-theme="dark"] .qt-demo { color-scheme: dark;
  --surface: #1a1a19; --text: #f0efec; --text-2: #c3c2b7; --grid: #34332f; --axis: #77766f;
  --exact: #3987e5; --model: #d95926; --ref: #c3c2b7; }
html, body { margin: 0; background: var(--surface, #fcfcfb); }
.qt-demo { background: var(--surface); color: var(--text); font: 15px/1.5 -apple-system, BlinkMacSystemFont,
  "Segoe UI", Roboto, Helvetica, Arial, sans-serif; max-width: 760px; margin: 0 auto; padding: 24px 16px 48px; }
.qt-demo h1 { font-size: 22px; line-height: 1.3; margin: 0 0 4px; }
.qt-demo h2 { font-size: 16px; margin: 28px 0 8px; }
.qt-demo .sub, .qt-demo .note, .qt-demo figcaption { color: var(--text-2); font-size: 13px; }
.qt-demo .hero { font-size: 17px; margin: 16px 0; }
.qt-demo svg { width: 100%; height: auto; display: block; }
.qt-demo svg text { fill: var(--text-2); font-size: 12px; }
.qt-demo .legend { display: flex; gap: 16px; font-size: 13px; color: var(--text-2); margin: 4px 0; }
.qt-demo .key { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; }
.qt-demo table { border-collapse: collapse; width: 100%; font-size: 13px; font-variant-numeric: tabular-nums; }
.qt-demo th, .qt-demo td { text-align: right; padding: 4px 6px; border-bottom: 1px solid var(--grid); }
.qt-demo th:first-child, .qt-demo td:first-child { text-align: left; }
.qt-demo .scroll { overflow-x: auto; }
.qt-demo code { font-size: 12px; overflow-wrap: anywhere; }
.qt-demo .attr { border-left: 3px solid var(--axis); padding-left: 10px; }
"""


def _e(s: object) -> str:
    return html.escape(str(s), quote=True)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _pp(x: float | None) -> str:
    if x is None:
        return "n/a"
    v = f"{x * 100:+.2f}"
    return "0.00" if float(v) == 0 else v


def _ci(c: Cell) -> str:
    if c.ci_lo is None or c.ci_hi is None:
        return "n/a"
    return f"[{c.ci_lo * 100:+.2f}, {c.ci_hi * 100:+.2f}]"


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * exp:
            return m * exp
    return 10 * exp  # pragma: no cover


def _ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    step = _nice_max((hi - lo) / n)
    start = math.ceil(lo / step) * step
    out = []
    v = start
    while v <= hi + 1e-12:
        out.append(round(v, 10))
        v += step
    return out


def _bars(keys: list[str], cells: dict[str, Cell]) -> str:
    iw, ih = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    vals = [v for k in keys for v in (cells[k].exact_fill_rate, cells[k].model_fill_rate) if v is not None]
    top = _nice_max(max(vals) if vals else 1.0)
    y = lambda v: PAD_T + ih * (1 - v / top)  # noqa: E731
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Fill rate by horizon: exact replay and model">']
    for t in _ticks(0, top):
        parts.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="var(--grid)"/>')
        parts.append(f'<text x="{PAD_L - 8}" y="{y(t) + 4:.1f}" text-anchor="end">{t * 100:g}%</text>')
    group = iw / max(1, len(keys))
    bw = min(28.0, group * 0.32)
    for i, k in enumerate(keys):
        cx = PAD_L + group * (i + 0.5)
        c = cells[k]
        label = duration_text(int(k))
        for j, (v, color, name) in enumerate(
            (
                (c.exact_fill_rate, "var(--exact)", "exact replay"),
                (c.model_fill_rate, "var(--model)", "NautilusTrader default"),
            )
        ):
            if v is None:
                continue
            x = cx - bw - 1 if j == 0 else cx + 1
            h = max(0.0, y(0) - y(v))
            r = min(4.0, bw / 2, h)
            yt = y(v)
            # rounded data end at the top, square at the baseline
            path = (
                f"M{x:.1f},{y(0):.1f} L{x:.1f},{yt + r:.1f} Q{x:.1f},{yt:.1f} {x + r:.1f},{yt:.1f} "
                f"L{x + bw - r:.1f},{yt:.1f} Q{x + bw:.1f},{yt:.1f} {x + bw:.1f},{yt + r:.1f} "
                f"L{x + bw:.1f},{y(0):.1f} Z"
            )
            parts.append(
                f'<path d="{path}" fill="{color}"><title>{_e(name)} within {_e(label)}: {_pct(v)}</title></path>'
            )
        parts.append(f'<text x="{cx:.1f}" y="{H - PAD_B + 18}" text-anchor="middle">{_e(label)}</text>')
    parts.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(0):.1f}" y2="{y(0):.1f}" stroke="var(--axis)"/>')
    parts.append(f'<text x="{PAD_L + iw / 2:.1f}" y="{H - 4}" text-anchor="middle">horizon after placement</text>')
    parts.append("</svg>")
    return "".join(parts)


def _bias(keys: list[str], cells: dict[str, Cell], ref: dict[str, tuple[float, float, float]]) -> str:
    iw, ih = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    vals = [0.0]
    for k in keys:
        c = cells[k]
        vals += [v for v in (c.bias, c.ci_lo, c.ci_hi) if v is not None]
        if k in ref:
            vals += list(ref[k])
    lo, hi = min(vals), max(vals)
    span = _nice_max(max(hi - lo, 1e-4))
    lo_t = math.floor(lo / (span / 4)) * (span / 4) if lo < 0 else 0.0
    hi_t = max(lo_t + span, math.ceil(hi / (span / 4)) * (span / 4))
    y = lambda v: PAD_T + ih * (hi_t - v) / (hi_t - lo_t)  # noqa: E731
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Fill-rate bias of the model by horizon, pp">']
    for t in _ticks(lo_t, hi_t):
        parts.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="var(--grid)"/>')
        tick = "0" if abs(t) < 1e-12 else f"{t * 100:+g}"
        parts.append(f'<text x="{PAD_L - 8}" y="{y(t) + 4:.1f}" text-anchor="end">{tick}</text>')
    parts.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(0):.1f}" y2="{y(0):.1f}" stroke="var(--axis)"/>')
    group = iw / max(1, len(keys))
    for i, k in enumerate(keys):
        cx = PAD_L + group * (i + 0.5)
        c = cells[k]
        label = duration_text(int(k))
        if c.ci_lo is not None and c.ci_hi is not None:
            parts.append(
                f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="{y(c.ci_lo):.1f}" y2="{y(c.ci_hi):.1f}" '
                f'stroke="var(--model)" stroke-width="2"/>'
            )
        if c.bias is not None:
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{y(c.bias):.1f}" r="5" fill="var(--model)" stroke="var(--surface)" '
                f'stroke-width="2"><title>within {_e(label)}: {_pp(c.bias)} pp, 95% CI {_ci(c)}</title></circle>'
            )
        if k in ref:
            v, rlo, rhi = ref[k]
            rx = cx + 16
            parts.append(
                f'<line x1="{rx:.1f}" x2="{rx:.1f}" y1="{y(rlo):.1f}" y2="{y(rhi):.1f}" stroke="var(--ref)" '
                f'stroke-width="2" stroke-dasharray="3 2"/>'
                f'<rect x="{rx - 4:.1f}" y="{y(v) - 4:.1f}" width="8" height="8" fill="var(--surface)" '
                f'stroke="var(--ref)" stroke-width="2" transform="rotate(45 {rx:.1f} {y(v):.1f})">'
                f"<title>published full session within {_e(label)}: {v * 100:+.2f} pp</title></rect>"
            )
        parts.append(f'<text x="{cx:.1f}" y="{H - PAD_B + 18}" text-anchor="middle">{_e(label)}</text>')
    parts.append(f'<text x="{PAD_L + iw / 2:.1f}" y="{H - 4}" text-anchor="middle">horizon after placement</text>')
    parts.append("</svg>")
    return "".join(parts)


def render_html(s: DemoSummary) -> str:
    """The page (module docstring)."""
    cells = s.headline.cells
    keys = sorted(cells, key=int)
    ref: dict[str, tuple[float, float, float]] = {}
    if s.reference is not None:
        for k, v in s.reference.bias.items():
            ref[k] = (v, s.reference.ci_lo[k], s.reference.ci_hi[k])
    src, win, data = s.source, s.window, s.data
    rows = "".join(
        f"<tr><td>{_e(duration_text(int(k)))}</td><td>{_pct(cells[k].exact_fill_rate)}</td>"
        f"<td>{_pct(cells[k].model_fill_rate)}</td><td>{_pp(cells[k].bias)}</td><td>{_ci(cells[k])}</td>"
        f"<td>{cells[k].n:,}</td></tr>"
        for k in keys
    )
    show = [k for k in ("1000000000", "10000000000", "60000000000") if k in cells] or keys[:3]

    def sort_key(m: object) -> tuple[float, str]:
        c = m.cells.get(show[-1])  # type: ignore[attr-defined]
        return (abs(c.bias) if c is not None and c.bias is not None else math.inf, m.model_id)  # type: ignore[attr-defined]

    others = "".join(
        f"<tr><td><code>{_e(m.model_id)}</code></td>"
        + "".join(f"<td>{_pp(m.cells[k].bias) if k in m.cells else 'n/a'}</td>" for k in show)
        + "</tr>"
        for m in sorted(s.models, key=sort_key)
    )
    attribution = ""
    if src.attribution:
        attribution = (
            f'<p class="attr">{_e(src.attribution)} '
            f'<a href="{_e(src.terms_url)}">IEX Historical Data Terms of Use</a></p>'
        )
    elif src.note:
        attribution = f'<p class="attr">{_e(src.note)}</p>'
    ref_note = ""
    if s.reference is not None:
        from queuetruth.demo.cli import reference_note

        extra = reference_note(s)
        ref_note = (
            f'<p class="note">Hollow diamonds: the published value for this day, {_e(s.reference.label)}.'
            + (f" {_e(extra[0].upper() + extra[1:])}." if extra else "")
            + "</p>"
        )
    location = src.location if is_url(src.location) else file_name(src.location)
    notes = list(s.warnings)
    if s.n_excluded_hbt_312:
        notes.append(
            f"{s.n_excluded_hbt_312:,} orders are left out of every model so that all are scored on the same "
            "orders (the zoo's common population): a faithful hftbacktest model stops on them (its issue #312)."
        )
    notes_html = (
        "<h2>Notes</h2><ul>" + "".join(f'<li class="note">{_e(n)}</li>' for n in notes) + "</ul>" if notes else ""
    )
    if data.cache_hit:
        dl = "subset from the local cache"
    else:
        verb = "downloaded" if data.remote else "read"
        size = f" of a {data.file_bytes / 1e9:,.2f} GB file" if data.file_bytes else ""
        dl = f"{data.bytes_downloaded / 1e6:,.1f} MB {verb}{size}"
    title = "Would these orders have filled?"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>queuetruth demo</title><style>{CSS}</style></head>
<body><main class="qt-demo">
<h1>{_e(title)}</h1>
<p class="sub">{_e(src.title)}, {_e(src.session_date)}, {_e(" ".join(data.symbols))}: {s.n_shadows:,} simulated resting
limit orders placed {_e(win.placement_start)} to {_e(win.placement_end)} ET, each followed for
{win.h_max_s // 60} minutes in the exact replay of the real queue.</p>
<p class="hero">{_e(s.headline.sentence)}</p>
<p class="note">Model: {_e(s.headline.verified_against)}.</p>
<h2>Fill rate by horizon</h2>
<div class="legend"><span><span class="key" style="background:var(--exact)"></span>exact replay (real queue)</span>
<span><span class="key" style="background:var(--model)"></span>{_e(s.headline.label)}</span></div>
<figure style="margin:0">{_bars(keys, cells)}</figure>
<h2>Overfill: model minus exact, percentage points</h2>
<figure style="margin:0">{_bias(keys, cells, ref)}
<figcaption>Dots: this run. Whiskers: 95% intervals from a cluster bootstrap over instrument x minute of placement
({s.interval.clusters} clusters).</figcaption></figure>
{ref_note}
<div class="scroll"><table>
<thead><tr><th>horizon</th><th>exact</th><th>NautilusTrader default</th><th>bias, pp</th><th>95% CI, pp</th>
<th>orders</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<h2>Every model in this run (bias, pp)</h2>
<div class="scroll"><table>
<thead><tr><th>model</th>{"".join(f"<th>{_e(duration_text(int(k)))}</th>" for k in show)}</tr></thead>
<tbody>{others}</tbody></table></div>
{notes_html}
<h2>Provenance</h2>
{attribution}
<p class="note">Source <code>{_e(location)}</code>, {_e(dl)}; subset of {data.kept_messages:,} messages
(sha256 <code>{_e(data.subset_sha256)}</code>), {data.records:,} records, data quality {_e(data.data_quality)}.
Data cut at {_e(win.cut)} ET. queuetruth {_e(s.queuetruth_version)}, run id <code>{_e(s.run_id)}</code>,
zoo run id <code>{_e(s.zoo_run_id)}</code>. Aggregate statistics only; no order-level rows are in this page.</p>
</main></body></html>
"""


__all__ = ["render_html"]
