"""Inline SVG chart helpers (server-side rendered, no JavaScript): line / step / point charts with CI bands or
whiskers, grouped bars, forest plots, diverging heatmaps, reliability panels with Wilson CIs and QQ plots.

Every chart is a standalone well-formed XML `<svg>` with a `<title>` and `<desc>`; colors come from CSS classes
(`s1`..`s4`, `grid`, `axis`, ...) defined by the page stylesheet, so light and dark themes switch without
re-rendering. Per-mark `<title>` elements give native hover tooltips. Output is a pure function of the arguments.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from queuetruth.report.fmt import coord, esc, num

Fmt = Callable[[float], str]

FONT_PX = 11
CHAR_W = 6.2  # approximate advance of the 11 px UI font, used for label layout


@dataclass
class Series:
    """One plotted series; `points` are (x, y) with y None for gaps; `ci` rows are (x, lo, hi)."""

    name: str
    points: Sequence[tuple[float, float | None]]
    slot: int = 1
    kind: str = "line"  # line | step | points
    ci: Sequence[tuple[float, float | None, float | None]] | None = None
    ci_style: str = "band"  # band | whisker
    markers: bool = False
    dashed: bool = False
    tips: Sequence[str] | None = None


@dataclass
class Ref:
    """A reference line at `value` on the x or y axis."""

    value: float
    label: str = ""
    cls: str = "ref"


@dataclass
class Frame:
    """Plot area geometry and scales."""

    width: float
    height: float
    left: float
    right: float
    top: float
    bottom: float
    x_lo: float = 0.0
    x_hi: float = 1.0
    y_lo: float = 0.0
    y_hi: float = 1.0
    x_log: bool = False
    x_ticks: list[float] = field(default_factory=list)
    y_ticks: list[float] = field(default_factory=list)

    @property
    def x0(self) -> float:
        return self.left

    @property
    def x1(self) -> float:
        return self.width - self.right

    @property
    def y0(self) -> float:
        return self.height - self.bottom

    @property
    def y1(self) -> float:
        return self.top

    def sx(self, x: float) -> float:
        if self.x_log:
            lo = math.log10(max(self.x_lo, 1e-300))
            hi = math.log10(max(self.x_hi, 1e-300))
            v = math.log10(max(x, 1e-300)) if math.isfinite(x) else lo
        else:
            lo, hi, v = self.x_lo, self.x_hi, x
        return self.x0 + _frac(v, lo, hi) * (self.x1 - self.x0)

    def sy(self, y: float) -> float:
        return self.y0 - _frac(y, self.y_lo, self.y_hi) * (self.y0 - self.y1)


def _frac(v: float, lo: float, hi: float) -> float:
    """Position of v in [lo, hi] as a fraction (clamped to [-10, 11]); 0.5 for a degenerate or non-finite range or
    value."""
    d = hi - lo
    if not (math.isfinite(v) and math.isfinite(lo) and math.isfinite(d)) or d == 0:
        return 0.5
    f = (v - lo) / d
    if not math.isfinite(f):
        return 0.5
    return min(max(f, -10.0), 11.0)  # far outside the plot area either way; keeps coordinates finite


def fin(v: float | None) -> float | None:
    """The value when it is a finite number, else None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def safe_range(lo: float, hi: float) -> tuple[float, float]:
    """A finite range with lo < hi: non-finite -> [0, 1]; empty or relatively negligible widths are padded by 10%
    of the magnitude (1 around zero)."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return 0.0, 1.0
    if hi < lo:
        lo, hi = hi, lo
    scale = max(abs(lo), abs(hi))
    if hi - lo <= scale * 1e-9 or hi - lo < 1e-300:
        pad = scale * 0.1 if scale > 1e-290 else 1.0
        lo, hi = lo - pad, hi + pad
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        return 0.0, 1.0
    return lo, hi


def nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    """Round tick values (1, 2, 2.5, 5 x 10^k) covering [lo, hi]."""
    lo, hi = safe_range(lo, hi)
    raw = (hi - lo) / max(n, 1)
    if not (math.isfinite(raw) and raw > 0):
        return [lo, hi]
    mag = 10.0 ** math.floor(math.log10(raw))
    step = mag
    for m in (1.0, 2.0, 2.5, 5.0, 10.0):
        step = m * mag
        if raw <= step * (1 + 1e-9):
            break
    start = math.floor(lo / step + 1e-9) * step
    end = math.ceil(hi / step - 1e-9) * step
    count = round((end - start) / step) if math.isfinite((end - start) / step) else 0
    if count < 1 or count > 1000:
        return [lo, hi]
    ticks = sorted({round(start + i * step, 12) + 0.0 for i in range(count + 1)})
    if ticks and ticks[0] > lo:
        ticks.insert(0, round(ticks[0] - step, 12) + 0.0)
    if ticks and ticks[-1] < hi:
        ticks.append(round(ticks[-1] + step, 12) + 0.0)
    if len(ticks) < 2 or not ticks[-1] > ticks[0] or not all(math.isfinite(t) for t in ticks):
        return [lo, hi]
    return ticks


def log_ticks(lo: float, hi: float) -> tuple[float, float, list[float]]:
    """Decade ticks for a log axis covering [lo, hi]; adds 2 and 5 multiples on short ranges, thins long ranges to
    at most about 10 labels. Non-positive or non-finite bounds fall back to [1, 10]."""
    if not (math.isfinite(lo) and lo > 0):
        lo = 1.0
    lo = min(max(lo, 1e-300), 1e299)
    if not (math.isfinite(hi) and hi > lo):
        hi = lo * 10.0
    hi = min(hi, 1e300)
    a = math.floor(math.log10(lo) + 1e-12)
    b = math.ceil(math.log10(hi) - 1e-12)
    if b <= a:
        b = a + 1
    every = max(1, math.ceil((b - a) / 10))
    ticks = [10.0**k for k in range(a, b + 1, every)]
    if ticks[-1] < 10.0**b:
        ticks.append(10.0**b)
    if b - a <= 2:
        extra = [m * 10.0**k for k in range(a, b) for m in (2.0, 5.0)]
        ticks = sorted(ticks + extra)
    return 10.0**a, 10.0**b, ticks


def _svg_open(uid: str, frame: Frame, title: str, desc: str, cls: str = "chart") -> list[str]:
    w, h = coord(frame.width), coord(frame.height)
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" class="{cls}" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        f'role="img" aria-labelledby="{uid}-t {uid}-d">',
        f'<title id="{uid}-t">{esc(title)}</title>',
        f'<desc id="{uid}-d">{esc(desc)}</desc>',
    ]


def _text(x: float, y: float, s: str, cls: str = "tk", anchor: str = "middle", extra: str = "") -> str:
    return f'<text x="{coord(x)}" y="{coord(y)}" class="{cls}" text-anchor="{anchor}"{extra}>{esc(s)}</text>'


def _axes(fr: Frame, x_fmt: Fmt, y_fmt: Fmt, x_label: str, y_label: str, x_grid: bool = True) -> list[str]:
    out: list[str] = []
    for t in fr.y_ticks:
        y = fr.sy(t)
        out.append(f'<line class="grid" x1="{coord(fr.x0)}" x2="{coord(fr.x1)}" y1="{coord(y)}" y2="{coord(y)}"/>')
        out.append(_text(fr.x0 - 6, y + 3.5, y_fmt(t), anchor="end"))
    for t in fr.x_ticks:
        x = fr.sx(t)
        if x_grid:
            out.append(f'<line class="grid" x1="{coord(x)}" x2="{coord(x)}" y1="{coord(fr.y1)}" y2="{coord(fr.y0)}"/>')
        out.append(_text(x, fr.y0 + 15, x_fmt(t)))
    out.append(f'<line class="axis" x1="{coord(fr.x0)}" x2="{coord(fr.x1)}" y1="{coord(fr.y0)}" y2="{coord(fr.y0)}"/>')
    if x_label:
        out.append(_text((fr.x0 + fr.x1) / 2, fr.height - 4, x_label, cls="lab"))
    if y_label:
        out.append(
            _text(
                12,
                (fr.y0 + fr.y1) / 2,
                y_label,
                cls="lab",
                extra=f' transform="rotate(-90 12 {coord((fr.y0 + fr.y1) / 2)})"',
            )
        )
    return out


def _segments(fr: Frame, pts: Sequence[tuple[float, float | None]]) -> list[list[tuple[float, float]]]:
    segs: list[list[tuple[float, float]]] = [[]]
    for x, y in pts:
        if y is None or not math.isfinite(y) or not math.isfinite(x) or (fr.x_log and x <= 0):
            if segs[-1]:
                segs.append([])
            continue
        segs[-1].append((fr.sx(x), fr.sy(y)))
    return [s for s in segs if s]


def _path(seg: list[tuple[float, float]], step: bool) -> str:
    parts = [f"M{coord(seg[0][0])} {coord(seg[0][1])}"]
    for (_, py), (x, y) in itertools.pairwise(seg):
        if step:
            parts.append(f"H{coord(x)}")
            if y != py:
                parts.append(f"V{coord(y)}")
        else:
            parts.append(f"L{coord(x)} {coord(y)}")
    return "".join(parts)


def _domain(values: list[float], include: Sequence[float] = ()) -> tuple[float, float]:
    vals = [v for v in [*values, *include] if v is not None and math.isfinite(v)]
    if not vals:
        return 0.0, 1.0
    return min(vals), max(vals)


def line_chart(
    uid: str,
    title: str,
    desc: str,
    series: Sequence[Series],
    *,
    x_fmt: Fmt = num,
    y_fmt: Fmt = num,
    x_label: str = "",
    y_label: str = "",
    x_log: bool = False,
    x_range: tuple[float, float] | None = None,
    y_range: tuple[float, float] | None = None,
    y_include: Sequence[float] = (),
    hrefs: Sequence[Ref] = (),
    vrefs: Sequence[Ref] = (),
    xbands: Sequence[tuple[float, float, str]] = (),
    diag: bool = False,
    width: float = 640,
    height: float = 300,
    end_labels: bool = True,
    x_ticks: Sequence[float] | None = None,
) -> str:
    """Line / step / point chart with optional CI bands or whiskers, reference lines and a y = x diagonal; direct end
    labels for 2-4 series with short names (the HTML legend always carries identity)."""

    def ok_x(x: float | None) -> bool:
        return fin(x) is not None and not (x_log and x <= 0)  # type: ignore[operator]

    clean: list[Series] = []
    for s in series:
        keep = [i for i, (x, _) in enumerate(s.points) if ok_x(x)]
        clean.append(
            dataclasses.replace(
                s,
                points=[(float(s.points[i][0]), fin(s.points[i][1])) for i in keep],
                ci=None if s.ci is None else [(float(x), fin(lo), fin(hi)) for x, lo, hi in s.ci if ok_x(x)],
                tips=None if s.tips is None else [s.tips[i] for i in keep if i < len(s.tips)],
            )
        )
    series = clean
    hrefs = [r for r in hrefs if fin(r.value) is not None]
    vrefs = [r for r in vrefs if ok_x(r.value)]
    xbands = [b for b in xbands if fin(b[0]) is not None and fin(b[1]) is not None]
    if x_range is not None:
        x_range = safe_range(*x_range)
    if y_range is not None:
        y_range = safe_range(*y_range)
    ticks_given = sorted({float(t) for t in (x_ticks or ()) if fin(t) is not None})
    labelled = end_labels and 2 <= len(series) <= 4 and all(len(s.name) <= 18 for s in series)
    longest = max((len(s.name) for s in series), default=0)
    right = 16 + (min(longest, 18) * CHAR_W + 8 if labelled else 0)
    fr = Frame(width, height, 60, right, 14, 40, x_log=x_log)
    xs: list[float] = []
    ys: list[float] = []
    for s in series:
        for x, y in s.points:
            xs.append(x)
            if y is not None:
                ys.append(y)
        for _, lo, hi in s.ci or ():
            ys.extend(v for v in (lo, hi) if v is not None)
    ys.extend(r.value for r in hrefs)
    xs.extend(r.value for r in vrefs)
    xlo, xhi = x_range if x_range else _domain(xs)
    ylo, yhi = y_range if y_range else _domain(ys, y_include)
    if diag:
        lo, hi = min(xlo, ylo), max(xhi, yhi)
        xlo, xhi, ylo, yhi = lo, hi, lo, hi
    if len(ticks_given) >= 2:
        fr.x_ticks = ticks_given
        fr.x_lo, fr.x_hi = fr.x_ticks[0], fr.x_ticks[-1]
    elif x_log:
        xlo = xlo if xlo > 0 else 1.0
        xhi = xhi if xhi > xlo else xlo * 10
        fr.x_lo, fr.x_hi, fr.x_ticks = log_ticks(xlo, xhi)
    else:
        fr.x_ticks = nice_ticks(xlo, xhi, max(2, int((width - 80) // 90)))
        fr.x_lo, fr.x_hi = fr.x_ticks[0], fr.x_ticks[-1]
    if y_range:
        fr.y_ticks = [t for t in nice_ticks(*y_range, 5) if y_range[0] - 1e-12 <= t <= y_range[1] + 1e-12]
        fr.y_lo, fr.y_hi = y_range
    else:
        fr.y_ticks = nice_ticks(ylo, yhi, 5)
        fr.y_lo, fr.y_hi = fr.y_ticks[0], fr.y_ticks[-1]
    if diag:
        fr.y_lo, fr.y_hi, fr.y_ticks = fr.x_lo, fr.x_hi, list(fr.x_ticks)
    out = _svg_open(uid, fr, title, desc)
    for x0, x1, lab in xbands:
        a, b = fr.sx(max(x0, fr.x_lo)), fr.sx(min(x1, fr.x_hi))
        out.append(
            f'<rect class="xband" x="{coord(min(a, b))}" y="{coord(fr.y1)}" width="{coord(abs(b - a))}" '
            f'height="{coord(fr.y0 - fr.y1)}"><title>{esc(lab)}</title></rect>'
        )
    out += _axes(fr, x_fmt, y_fmt, x_label, y_label)
    if diag:
        out.append(
            f'<line class="diag" x1="{coord(fr.sx(fr.x_lo))}" y1="{coord(fr.sy(fr.y_lo))}" '
            f'x2="{coord(fr.sx(fr.x_hi))}" y2="{coord(fr.sy(fr.y_hi))}"/>'
        )
    for r in hrefs:
        y = fr.sy(r.value)
        out.append(f'<line class="{r.cls}" x1="{coord(fr.x0)}" x2="{coord(fr.x1)}" y1="{coord(y)}" y2="{coord(y)}"/>')
        if r.label:
            out.append(_text(fr.x0 + 4, y - 4, r.label, cls="reflab", anchor="start"))
    nlab = 0
    for r in vrefs:
        if x_log and r.value <= 0:
            continue
        x = fr.sx(r.value)
        out.append(f'<line class="{r.cls}" x1="{coord(x)}" x2="{coord(x)}" y1="{coord(fr.y1)}" y2="{coord(fr.y0)}"/>')
        if r.label:
            out.append(_text(x + 4, fr.y1 + 10 + 12 * (nlab % 2), r.label, cls="reflab", anchor="start"))
            nlab += 1
    ends: list[tuple[float, str, int]] = []
    for s in series:
        cls = f"s{s.slot}"
        if s.ci and s.ci_style == "band":
            pts = [(x, lo, hi) for x, lo, hi in s.ci if lo is not None and hi is not None and not (x_log and x <= 0)]
            if len(pts) >= 2:
                up = [f"{coord(fr.sx(x))} {coord(fr.sy(hi))}" for x, _, hi in pts]
                dn = [f"{coord(fr.sx(x))} {coord(fr.sy(lo))}" for x, lo, _ in reversed(pts)]
                out.append(f'<path class="band {cls}" d="M{"L".join(up)}L{"L".join(dn)}Z"/>')
        if s.ci and (s.ci_style == "whisker" or len(s.ci) == 1):
            for x, lo, hi in s.ci:
                if lo is None or hi is None or (x_log and x <= 0):
                    continue
                X = coord(fr.sx(x))
                out.append(
                    f'<line class="wh {cls}" x1="{X}" x2="{X}" y1="{coord(fr.sy(lo))}" y2="{coord(fr.sy(hi))}"/>'
                )
        if s.kind != "points":
            dash = " dash" if s.dashed else ""
            for seg in _segments(fr, s.points):
                if len(seg) >= 2:
                    out.append(f'<path class="ln {cls}{dash}" d="{_path(seg, s.kind == "step")}"/>')
        if s.markers or s.kind == "points":
            for i, (x, y) in enumerate(s.points):
                if y is None or (x_log and x <= 0):
                    continue
                tip = s.tips[i] if s.tips and i < len(s.tips) else f"{s.name}: {x_fmt(x)}, {y_fmt(y)}"
                out.append(
                    f'<circle class="mk {cls}" cx="{coord(fr.sx(x))}" cy="{coord(fr.sy(y))}" r="4">'
                    f"<title>{esc(tip)}</title></circle>"
                )
        if labelled:
            last = [(x, y) for x, y in s.points if y is not None and not (x_log and x <= 0)]
            if last:
                ends.append((fr.sy(last[-1][1]), s.name, s.slot))
    if ends:
        ends.sort()
        placed: list[float] = []
        for y, _, _ in ends:
            y = min(max(y, fr.y1 + 4), fr.y0)
            if placed and y - placed[-1] < 13:
                y = placed[-1] + 13
            placed.append(y)
        for (_, name, slot), y in zip(ends, placed, strict=True):
            label = name if len(name) <= 18 else name[:17] + "…"
            out.append(_text(fr.x1 + 6, y + 3.5, label, cls=f"endlab t{slot}", anchor="start"))
    out.append("</svg>")
    return "".join(out)


def _bar_path(x: float, w: float, y_base: float, y_end: float, r: float = 3.0) -> str:
    """Bar with the end away from the baseline rounded."""
    h = abs(y_end - y_base)
    r = min(r, w / 2, h)
    if h < 0.5:
        return f"M{coord(x)} {coord(y_base)}H{coord(x + w)}"
    if y_end < y_base:  # grows upward
        return (
            f"M{coord(x)} {coord(y_base)}V{coord(y_end + r)}Q{coord(x)} {coord(y_end)} {coord(x + r)} {coord(y_end)}"
            f"H{coord(x + w - r)}Q{coord(x + w)} {coord(y_end)} {coord(x + w)} {coord(y_end + r)}"
            f"V{coord(y_base)}Z"
        )
    return (
        f"M{coord(x)} {coord(y_base)}V{coord(y_end - r)}Q{coord(x)} {coord(y_end)} {coord(x + r)} {coord(y_end)}"
        f"H{coord(x + w - r)}Q{coord(x + w)} {coord(y_end)} {coord(x + w)} {coord(y_end - r)}"
        f"V{coord(y_base)}Z"
    )


def bar_chart(
    uid: str,
    title: str,
    desc: str,
    categories: Sequence[str],
    groups: Sequence[tuple[str, int, Sequence[float | None]]],
    *,
    y_fmt: Fmt = num,
    x_label: str = "",
    y_label: str = "",
    tips: Sequence[Sequence[str]] | None = None,
    width: float = 640,
    height: float = 260,
) -> str:
    """Grouped vertical bars; `groups` = (name, slot, value per category); 2 px surface gap between bars.
    Non-finite values are treated as missing."""
    groups = [(name, slot, [fin(v) for v in vs]) for name, slot, vs in groups]
    fr = Frame(width, height, 60, 16, 14, 40)
    vals = [v for _, _, vs in groups for v in vs if v is not None]
    lo, hi = _domain(vals, (0.0,))
    fr.y_ticks = nice_ticks(lo, hi, 5)
    fr.y_lo, fr.y_hi = fr.y_ticks[0], fr.y_ticks[-1]
    out = _svg_open(uid, fr, title, desc)
    for t in fr.y_ticks:
        y = fr.sy(t)
        out.append(f'<line class="grid" x1="{coord(fr.x0)}" x2="{coord(fr.x1)}" y1="{coord(y)}" y2="{coord(y)}"/>')
        out.append(_text(fr.x0 - 6, y + 3.5, y_fmt(t), anchor="end"))
    n = max(len(categories), 1)
    band = (fr.x1 - fr.x0) / n
    gw = band * 0.78
    bw = max((gw - 2 * (len(groups) - 1)) / max(len(groups), 1), 1.0)
    base = fr.sy(0.0)
    for ci_, cat in enumerate(categories):
        bx = fr.x0 + ci_ * band + (band - gw) / 2
        for gi, (name, slot, vs) in enumerate(groups):
            v = vs[ci_] if ci_ < len(vs) else None
            if v is None:
                continue
            x = bx + gi * (bw + 2)
            tip = tips[gi][ci_] if tips else f"{name}, {cat}: {y_fmt(v)}"
            out.append(
                f'<path class="bar s{slot}" d="{_bar_path(x, bw, base, fr.sy(v))}"><title>{esc(tip)}</title></path>'
            )
        out.append(_text(fr.x0 + ci_ * band + band / 2, fr.y0 + 15, cat))
    out.append(f'<line class="axis" x1="{coord(fr.x0)}" x2="{coord(fr.x1)}" y1="{coord(base)}" y2="{coord(base)}"/>')
    if x_label:
        out.append(_text((fr.x0 + fr.x1) / 2, fr.height - 4, x_label, cls="lab"))
    if y_label:
        mid = coord((fr.y0 + fr.y1) / 2)
        out.append(_text(12, (fr.y0 + fr.y1) / 2, y_label, cls="lab", extra=f' transform="rotate(-90 12 {mid})"'))
    out.append("</svg>")
    return "".join(out)


def forest_chart(
    uid: str,
    title: str,
    desc: str,
    rows: Sequence[tuple[str, float | None, float | None, float | None, str]],
    *,
    x_fmt: Fmt = num,
    x_label: str = "",
    band: tuple[float, float] | None = None,
    band_label: str = "",
    width: float = 640,
    slot: int = 1,
    alt: Sequence[float | None] | None = None,
    alt_slot: int = 3,
) -> str:
    """Horizontal point estimates with CI whiskers, one row per (label, est, lo, hi, tooltip); zero line; optional
    second estimate per row (`alt`) drawn as a diamond. Non-finite values are treated as missing."""
    rows = [(lab, fin(est), fin(lo_), fin(hi_), tip) for lab, est, lo_, hi_, tip in rows]
    alt = None if alt is None else [fin(a) for a in alt]
    label_w = min(max((len(r[0]) for r in rows), default=4) * CHAR_W + 16, 300)
    fr = Frame(width, 26 + 20 * max(len(rows), 1) + 34, label_w, 20, 16, 34)
    vals = [v for r in rows for v in r[1:4] if v is not None] + [v for v in (alt or []) if v is not None]
    extra = [0.0] + (list(band) if band else [])
    lo, hi = _domain(vals, extra)
    fr.x_ticks = nice_ticks(lo, hi, max(2, int((width - label_w) // 80)))
    fr.x_lo, fr.x_hi = fr.x_ticks[0], fr.x_ticks[-1]
    out = _svg_open(uid, fr, title, desc)
    if band:
        a, b = fr.sx(band[0]), fr.sx(band[1])
        out.append(
            f'<rect class="xband" x="{coord(a)}" y="{coord(fr.y1)}" width="{coord(b - a)}" '
            f'height="{coord(fr.y0 - fr.y1)}"><title>{esc(band_label)}</title></rect>'
        )
    for t in fr.x_ticks:
        x = fr.sx(t)
        out.append(f'<line class="grid" x1="{coord(x)}" x2="{coord(x)}" y1="{coord(fr.y1)}" y2="{coord(fr.y0)}"/>')
        out.append(_text(x, fr.y0 + 15, x_fmt(t)))
    z = fr.sx(0.0)
    out.append(f'<line class="zero" x1="{coord(z)}" x2="{coord(z)}" y1="{coord(fr.y1)}" y2="{coord(fr.y0)}"/>')
    for i, (lab, est, clo, chi, tip) in enumerate(rows):
        y = fr.y1 + 10 + 20 * i
        out.append(_text(fr.x0 - 8, y + 3.5, lab if len(lab) <= 46 else lab[:45] + "…", cls="rowlab", anchor="end"))
        g = [f"<g><title>{esc(tip)}</title>"]
        if clo is not None and chi is not None:
            g.append(
                f'<line class="wh s{slot}" x1="{coord(fr.sx(clo))}" x2="{coord(fr.sx(chi))}" '
                f'y1="{coord(y)}" y2="{coord(y)}"/>'
            )
        if est is not None:
            g.append(f'<circle class="mk s{slot}" cx="{coord(fr.sx(est))}" cy="{coord(y)}" r="4.5"/>')
        a = alt[i] if alt is not None and i < len(alt) else None
        if a is not None:
            ax = fr.sx(a)
            g.append(
                f'<path class="mk s{alt_slot}" d="M{coord(ax)} {coord(y - 5.5)}L{coord(ax + 5.5)} {coord(y)}'
                f'L{coord(ax)} {coord(y + 5.5)}L{coord(ax - 5.5)} {coord(y)}Z"/>'
            )
        g.append("</g>")
        out.append("".join(g))
    if x_label:
        out.append(_text((fr.x0 + fr.x1) / 2, fr.height - 4, x_label, cls="lab"))
    out.append("</svg>")
    return "".join(out)


def heatmap(
    uid: str,
    title: str,
    desc: str,
    row_labels: Sequence[str],
    col_labels: Sequence[str],
    values: Sequence[Sequence[float | None]],
    *,
    fmt: Fmt = num,
    tips: Sequence[Sequence[str]] | None = None,
    x_label: str = "",
    vmax: float | None = None,
    cell_w: float = 50,
) -> str:
    """Diverging heatmap: negative cells tinted `--div-neg`, positive `--div-pos`, opacity by |value| / vmax (shared
    across charts when given). Non-finite values are treated as missing."""
    values = [[fin(v) for v in row] for row in values]
    vmax = fin(vmax)
    label_w = min(max((len(r) for r in row_labels), default=4) * CHAR_W + 16, 300)
    ncol = max(len(col_labels), 1)
    cw = cell_w
    width = label_w + 12 + cw * ncol
    ch = 20.0
    height = 22 + ch * max(len(row_labels), 1) + 30
    fr = Frame(width, height, label_w, 12, 22, 30)
    if not vmax or vmax <= 0:
        vmax = max((abs(v) for row in values for v in row if v is not None), default=0.0) or 1.0
    out = _svg_open(uid, fr, title, desc)
    for j, c in enumerate(col_labels):
        out.append(_text(fr.x0 + cw * j + cw / 2, fr.y1 - 7, c))
    for i, lab in enumerate(row_labels):
        y = fr.y1 + ch * i
        out.append(
            _text(fr.x0 - 8, y + ch / 2 + 3.5, lab if len(lab) <= 46 else lab[:45] + "…", cls="rowlab", anchor="end")
        )
        for j in range(len(col_labels)):
            v = values[i][j] if j < len(values[i]) else None
            x = fr.x0 + cw * j
            tip = tips[i][j] if tips else f"{lab}, {col_labels[j]}: {fmt(v) if v is not None else 'no data'}"
            cell = [f"<g><title>{esc(tip)}</title>"]
            cell.append(
                f'<rect class="hm0" x="{coord(x + 1)}" y="{coord(y + 1)}" width="{coord(cw - 2)}" '
                f'height="{coord(ch - 2)}"/>'
            )
            if v is not None:
                op = min(abs(v) / vmax, 1.0) * 0.8
                pole = "hmp" if v > 0 else "hmn"
                cell.append(
                    f'<rect class="{pole}" x="{coord(x + 1)}" y="{coord(y + 1)}" width="{coord(cw - 2)}" '
                    f'height="{coord(ch - 2)}" fill-opacity="{op:.3f}"/>'
                )
                if cw >= 34:
                    cell.append(_text(x + cw / 2, y + ch / 2 + 3.5, fmt(v), cls="cell"))
            cell.append("</g>")
            out.append("".join(cell))
    if x_label:
        out.append(_text((fr.x0 + fr.x1) / 2, fr.height - 8, x_label, cls="lab"))
    out.append("</svg>")
    return "".join(out)


@dataclass
class RelBin:
    """One reliability bin: bin range, count, mean forecast, observed frequency and its Wilson CI."""

    lo: float
    hi: float
    n: int
    p_mean: float
    obs: float
    ci_lo: float
    ci_hi: float


def reliability_panel(
    uid: str, title: str, desc: str, bins: Sequence[RelBin], *, label: str = "", size: float = 190, slot: int = 1
) -> str:
    """Reliability diagram: observed vs forecast with Wilson 95% whiskers, y = x diagonal and a count strip;
    `label` is the visible panel heading (defaults to the title)."""
    fr = Frame(size, size + 26, 34, 8, 20, 50)
    fr.x_lo, fr.x_hi, fr.y_lo, fr.y_hi = 0.0, 1.0, 0.0, 1.0
    fr.x_ticks = fr.y_ticks = [0.0, 0.5, 1.0]
    out = _svg_open(uid, fr, title, desc, cls="chart rel")
    shown = label or title
    short = shown if len(shown) <= 34 else shown[:33] + "\u2026"
    out.append(_text(fr.x0, 12, short, cls="ptitle", anchor="start"))
    for t in fr.y_ticks:
        y = fr.sy(t)
        out.append(f'<line class="grid" x1="{coord(fr.x0)}" x2="{coord(fr.x1)}" y1="{coord(y)}" y2="{coord(y)}"/>')
        out.append(_text(fr.x0 - 4, y + 3.5, num(t), anchor="end"))
        x = fr.sx(t)
        out.append(_text(x, fr.y0 + 13, num(t)))
    out.append(
        f'<line class="diag" x1="{coord(fr.sx(0))}" y1="{coord(fr.sy(0))}" x2="{coord(fr.sx(1))}" '
        f'y2="{coord(fr.sy(1))}"/>'
    )
    nmax = max((b.n for b in bins), default=0)
    strip_top = fr.y0 + 18
    for b in bins:
        if nmax > 0 and b.n > 0:
            h = 14.0 * math.sqrt(b.n / nmax)
            x0, x1 = fr.sx(b.lo) + 1, fr.sx(b.hi) - 1
            out.append(
                f'<rect class="cnt" x="{coord(x0)}" y="{coord(strip_top + 14 - h)}" width="{coord(max(x1 - x0, 0.5))}" '
                f'height="{coord(h)}"><title>{esc(f"p in [{b.lo:.1f}, {b.hi:.1f}]: n = {b.n:,}")}</title></rect>'
            )
    for b in bins:
        if b.n == 0 or any(fin(v) is None for v in (b.p_mean, b.obs, b.ci_lo, b.ci_hi)):
            continue
        x, y = fr.sx(b.p_mean), fr.sy(b.obs)
        tip = (
            f"forecast {b.p_mean:.3f}, observed {b.obs:.3f} (95% Wilson CI [{b.ci_lo:.3f}, {b.ci_hi:.3f}]), n = {b.n:,}"
        )
        out.append(
            f"<g><title>{esc(tip)}</title>"
            f'<line class="wh s{slot}" x1="{coord(x)}" x2="{coord(x)}" y1="{coord(fr.sy(b.ci_lo))}" '
            f'y2="{coord(fr.sy(b.ci_hi))}"/>'
            f'<circle class="mk s{slot}" cx="{coord(x)}" cy="{coord(y)}" r="3.5"/></g>'
        )
    out.append(_text((fr.x0 + fr.x1) / 2, fr.height - 2, "forecast probability", cls="lab"))
    out.append("</svg>")
    return "".join(out)


TIME_STEPS_S = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)


def time_ticks(t0_ns: int, span_s: float, n: int = 6) -> tuple[list[float], Fmt]:
    """Clock-aligned ticks (seconds relative to `t0_ns`) and their UTC label formatter."""
    span_s = max(span_s, 1.0) if math.isfinite(span_s) else 1.0
    step = next((st for st in TIME_STEPS_S if span_s / st <= n), 86400 * math.ceil(span_s / 86400 / n))
    t0s = t0_ns / 1e9
    a = math.floor(t0s / step) * step
    b = math.ceil((t0s + span_s) / step) * step
    ticks = [float(v - t0s) for v in range(int(a), int(b) + 1, int(step))]
    pattern = "%m-%d %H:%M" if span_s > 86400 else ("%H:%M:%S" if step < 60 else "%H:%M")

    def fmt_(v: float) -> str:
        return time.strftime(pattern, time.gmtime(math.floor(t0s + v + 1e-6)))

    return ticks, fmt_
