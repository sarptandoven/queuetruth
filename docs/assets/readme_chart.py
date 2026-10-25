"""Render the README chart from the numbers published in docs/RESULTS.md.

Usage:
    python docs/assets/readme_chart.py            # (re)write docs/assets/fill-bias-1s.svg
    python docs/assets/readme_chart.py --check    # exit 1 if the SVG is missing or stale
    python docs/assets/readme_chart.py --self-test  # parser and renderer checks, including failure modes

The chart is the 1 s column of the faithful-mode leaderboard in docs/RESULTS.md section 4 ("How far are L2 queue
models from exact truth?"): fill-rate bias against the exact replay, in percentage points with the 95% day-block
bootstrap CI, for each queue model on both public Nasdaq days. The numbers are parsed from that Markdown table, not
typed here, so the chart cannot drift from the published results: a change to the table without a rerun of this
script fails `--check`. Two kinds of row are left out and named in the chart's footnote: the bounds
(`front_of_queue`, `trade_through_only`, which are not models) and, when the table has it, `hbt_power[n=1]`, which
equals `hbt_power3[n=1]` in every cell and is no longer in `--models all`.

The section's "hftbacktest #312 aborts" table is parsed too. While its title says the aborted shadows were left out
of each model's own metrics (the queuetruth 0.1.0 rule; 0.1.1 scores every model on one common population), the rows
it names are marked and the footnote states, from that table, how many orders each day's marked rows rest on.

Standard library only; the output is byte-for-byte deterministic. Exit codes: 0 ok, 1 stale SVG (`--check`) or a
failed self-test, 2 RESULTS.md unreadable (QT-INPUT-001) or without the expected tables (QT-INPUT-002).
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import textwrap
from dataclasses import dataclass
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
RESULTS = ROOT / "docs" / "RESULTS.md"
OUT = HERE / "fill-bias-1s.svg"

SECTION = "## 4. How far are L2 queue models from exact truth?"
TABLE_TITLE = "**faithful mode** (fill-rate bias in pp, 95% CI)"
ABORTS_TITLE = "**hftbacktest #312 aborts**"
# The abort table's title when each model left out only its own aborts (the 0.1.0 rule), e.g.
# "(faithful mode, shadows of 3,000 left out of the model's metrics)".
PER_MODEL = re.compile(r"shadows of ([\d,]+) left out of the model's metrics")
DAYS = ("2019-12-30", "2020-01-30")
HORIZON = "1 s"
LEFT_OUT = ("front_of_queue", "trade_through_only", "hbt_power[n=1]")
L3_INPUT = ("hbt_l3_fifo",)  # models that read the MBO book, not the L2 view
MARK = "†"  # dagger: rows resting on fewer orders than the population
CELL = re.compile(r"^([+-]\d+\.\d+) \[([+-]\d+\.\d+), ([+-]\d+\.\d+)\]$")
COUNT = re.compile(r"^\d[\d,]*$")


@dataclass(frozen=True)
class Est:
    """One published estimate: bias and its 95% CI, in percentage points."""

    value: float
    lo: float
    hi: float


@dataclass(frozen=True)
class Row:
    """One model: its id as published and one estimate per day."""

    model: str
    days: tuple[Est, Est]


@dataclass(frozen=True)
class Aborts:
    """The per-model #312 exclusion: population per day and the shadows each model left out, per day."""

    population: int
    left_out: dict[str, tuple[int, int]]


@dataclass(frozen=True)
class Chart:
    rows: list[Row]
    aborts: Aborts | None  # None: every row uses the full population
    skipped: tuple[str, ...] = ()  # LEFT_OUT rows the table has, in table order (named in the footnote)


class ChartError(Exception):
    """RESULTS.md no longer has the table this chart is drawn from, or the table is malformed."""


class SelfTestError(Exception):
    """A self-test expectation did not hold."""


def parse_cell(text: str, where: str) -> Est:
    m = CELL.match(text.strip())
    if not m:
        raise ChartError(f"{where}: cannot parse '{text.strip()}' as 'value [lo, hi]'")
    v, lo, hi = (float(g) for g in m.groups())
    if not lo <= v <= hi:
        raise ChartError(f"{where}: estimate {v} outside its CI [{lo}, {hi}]")
    return Est(v, lo, hi)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _table(lines: list[str], title: int, name: str, label: str) -> tuple[int, list[str], list[tuple[int, list[str]]]]:
    """The first table after line `title`: its header line index, header cells and (line index, cells) per row."""
    i = title + 1
    while i < len(lines) and not lines[i].startswith("|"):
        if lines[i].startswith("#") or lines[i].startswith("**"):
            break
        i += 1
    if i >= len(lines) or not lines[i].startswith("|"):
        raise ChartError(f"{name}:{title + 1}: no table after '{label}'")
    header = _cells(lines[i])
    body: list[tuple[int, list[str]]] = []
    seen: set[str] = set()
    for j in range(i + 2, len(lines)):
        if not lines[j].startswith("|"):
            break
        cells = _cells(lines[j])
        if len(cells) != len(header):
            raise ChartError(f"{name}:{j + 1}: {len(cells)} cells where the header has {len(header)}")
        model = cells[0].strip("`")
        if model in seen:
            raise ChartError(f"{name}:{j + 1}: model '{model}' appears twice in the table")
        seen.add(model)
        body.append((j, cells))
    return i, header, body


def _section_end(lines: list[str], start: int) -> int:
    return next((k for k in range(start + 1, len(lines)) if lines[k].startswith("## ")), len(lines))


def parse_aborts(lines: list[str], start: int, end: int, name: str) -> Aborts | None:
    """The section's per-model #312 abort table, or None when the section does not use the per-model rule."""
    title = next((k for k in range(start, end) if lines[k].startswith(ABORTS_TITLE)), None)
    if title is None:
        return None
    m = PER_MODEL.search(lines[title])
    if m is None:
        return None
    population = int(m.group(1).replace(",", ""))
    i, header, body = _table(lines, title, name, ABORTS_TITLE)
    missing = [d for d in DAYS if d not in header]
    if missing:
        raise ChartError(f"{name}:{i + 1}: abort table header lacks column(s) {', '.join(missing)}")
    cols = [header.index(d) for d in DAYS]
    left_out: dict[str, tuple[int, int]] = {}
    for j, cells in body:
        counts = []
        for c in cols:
            if not COUNT.match(cells[c]):
                raise ChartError(f"{name}:{j + 1}: cannot parse '{cells[c]}' as a count")
            n = int(cells[c].replace(",", ""))
            if n > population:
                raise ChartError(f"{name}:{j + 1}: {n} aborts exceed the population of {population}")
            counts.append(n)
        left_out[cells[0].strip("`")] = (counts[0], counts[1])
    return Aborts(population, left_out)


def parse_results(text: str, name: str = "RESULTS.md") -> Chart:
    """The faithful-mode 1 s rows of RESULTS.md section 4, in table order, and its #312 abort table (`name` labels
    errors)."""
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith(SECTION))
    except StopIteration:
        raise ChartError(f"{name}: section '{SECTION}' not found") from None
    end = _section_end(lines, start)
    title = next((i for i in range(start, end) if lines[i].strip() == TABLE_TITLE), None)
    if title is None:
        raise ChartError(f"{name}: table '{TABLE_TITLE}' not found in section '{SECTION}'")
    i, header, body = _table(lines, title, name, TABLE_TITLE)
    missing = [f"'{d} {HORIZON}'" for d in DAYS if f"{d} {HORIZON}" not in header]
    if missing:
        raise ChartError(f"{name}:{i + 1}: header lacks column(s) {', '.join(missing)}")
    cols = [header.index(f"{d} {HORIZON}") for d in DAYS]
    rows: list[Row] = []
    skipped: list[str] = []
    for j, cells in body:
        model = cells[0].strip("`")
        if model in LEFT_OUT:
            skipped.append(model)
            continue
        where = f"{name}:{j + 1}"
        rows.append(Row(model, (parse_cell(cells[cols[0]], where), parse_cell(cells[cols[1]], where))))
    if len(rows) < 2:
        raise ChartError(f"{name}:{i + 1}: fewer than 2 model rows in the faithful table")
    aborts = parse_aborts(lines, start, end, name)
    if aborts is not None:
        charted = {r.model for r in rows}
        stray = sorted(set(aborts.left_out) - charted - set(LEFT_OUT))
        if stray:
            raise ChartError(f"{name}: abort table names model(s) not in the faithful table: {', '.join(stray)}")
    return Chart(rows, aborts, tuple(skipped))


def short_id(model: str) -> str:
    """The id as typed on the command line: keys at their default (`particles=100`) left out."""
    return model.replace(";particles=100]", "]")


def label(model: str, marked: bool) -> str:
    text = short_id(model)
    if model in L3_INPUT:
        text += " (L3 input)"
    return f"{text} {MARK}" if marked else text


# Layout, in SVG user units (px at 100% zoom).
W = 760
PAD = 24
LABEL_W = 196
PLOT_L = PAD + LABEL_W
PLOT_R = W - PAD - 56
ROW_H = 26
SUB_WRAP = 104  # characters per subtitle line (13 px)
NOTE_WRAP = 118  # characters per footnote line (11.5 px)
MIN_HALF_RANGE = 4  # the axis spans at least -4 .. +4 pp, symmetric so "under" and "over" read alike

STYLE = """
.bg{fill:#fcfcfb}.t1{fill:#0b0b0b}.t2{fill:#52514e}.mu{fill:#6b6a65}
.grid{stroke:#e1e0d9;stroke-width:1}.zero{stroke:#52514e;stroke-width:1.5}
.d1{fill:#2a78d6}.d2{fill:#d0571f}.c1{stroke:#2a78d6}.c2{stroke:#d0571f}
.ring{stroke:#fcfcfb;stroke-width:2}.ci{stroke-width:2;stroke-linecap:round}
text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){
.bg{fill:#1a1a19}.t1{fill:#f0efec}.t2{fill:#c3c2b7}.mu{fill:#a3a29b}
.grid{stroke:#2c2c2a}.zero{stroke:#c3c2b7}
.d1{fill:#3987e5}.d2{fill:#e06a37}.c1{stroke:#3987e5}.c2{stroke:#e06a37}.ring{stroke:#1a1a19}}
""".strip()


def axis(rows: list[Row]) -> tuple[int, int]:
    """Half range of the symmetric axis and its tick step: every estimate and CI bound lies inside, none clamped."""
    extreme = max(max(abs(e.lo), abs(e.hi)) for r in rows for e in r.days)
    half = max(MIN_HALF_RANGE, math.ceil(extreme))
    step = 1 if half <= 6 else 2 if half <= 12 else 5
    half = math.ceil(half / step) * step
    return half, step


def f(v: float) -> str:
    """Coordinates with at most one decimal, no trailing zeros: stable text across platforms."""
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def n(v: int) -> str:
    return f"{v:,}"


def footnote(chart: Chart) -> list[str]:
    """The footnote paragraphs, all generated from the parsed tables."""
    same = ", and hbt_power[n=1], which equals hbt_power3[n=1]" if "hbt_power[n=1]" in chart.skipped else ""
    notes = [
        "Source: docs/RESULTS.md section 4, faithful table, 1 s columns. Not shown: the bounds front_of_queue and "
        f"trade_through_only{same}."
    ]
    marked = marked_rows(chart)
    if marked and chart.aborts is not None:
        pop = chart.aborts.population
        spans = []
        for k in range(len(DAYS)):
            kept = [pop - chart.aborts.left_out[m][k] for m in marked]
            lo, hi = min(kept), max(kept)
            spans.append(n(lo) if lo == hi else f"{n(lo)} to {n(hi)}")
        notes.append(
            f"{MARK} Each marked row leaves out that model's own hftbacktest #312 aborts (the queuetruth 0.1.0 rule), "
            f"so it rests on {spans[0]} of {n(pop)} orders on {DAYS[0]} and {spans[1]} on {DAYS[1]}. queuetruth 0.1.1 "
            "scores every model on "
            "one common population; these cells move when the table is rerun."
        )
    notes.append("Rendered by docs/assets/readme_chart.py.")
    return notes


def wrap(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, width, break_long_words=False, break_on_hyphens=False)


def marked_rows(chart: Chart) -> list[str]:
    if chart.aborts is None:
        return []
    return sorted(r.model for r in chart.rows if r.model in chart.aborts.left_out)


def render(chart: Chart) -> str:
    rows = sorted(chart.rows, key=lambda r: (-r.days[0].value, r.model))
    marked = set(marked_rows(chart))
    half, step = axis(rows)
    x_min, x_max = -half, half

    def x_of(v: float) -> float:
        if not x_min <= v <= x_max:  # axis() covers every value; this guards future edits to it
            raise ChartError(f"value {v} outside the axis [{x_min}, {x_max}]")
        return PLOT_L + (v - x_min) / (x_max - x_min) * (PLOT_R - PLOT_L)

    pop = chart.aborts.population if chart.aborts else None
    orders = "Small orders" if pop is None else f"{n(pop)} small orders per day ({MARK} rows: fewer)"
    subtitle = wrap(
        "Fill-rate bias at 1 s, model minus exact, in percentage points with day-block bootstrap 95% CI. "
        f"{orders} at or near the touch of AAPL, MSFT, SPY and TSLA on two public Nasdaq TotalView-ITCH days, "
        "faithful mode.",
        SUB_WRAP,
    )
    legend_y = 56 + 18 * len(subtitle) + 3
    plot_top = legend_y + 21
    plot_bot = plot_top + ROW_H * len(rows)
    notes = footnote(chart)
    note_lines = [ln for note in notes for ln in wrap(note, NOTE_WRAP)]
    h = plot_bot + 58 + 16 * len(note_lines)
    out: list[str] = []
    a = out.append
    a(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {h}" width="{W}" height="{h}" role="img" '
        'aria-labelledby="qt-title qt-desc">'
    )
    a('<title id="qt-title">Queue models against the exact replay: fill-rate bias at 1 s</title>')
    desc = "; ".join(
        f"{label(r.model, r.model in marked)}: {r.days[0].value:+.2f} pp on {DAYS[0]}, "
        f"{r.days[1].value:+.2f} pp on {DAYS[1]}"
        for r in rows
    )
    a(f'<desc id="qt-desc">{escape(desc)}. {escape(" ".join(notes))}</desc>')
    a(f"<style>{STYLE}</style>")
    a(f'<rect class="bg" x="0" y="0" width="{W}" height="{h}" rx="8"/>')
    a(
        f'<text class="t1" x="{PAD}" y="34" font-size="17" font-weight="600">'
        "How far queue models are from the exact replay</text>"
    )
    for k, ln in enumerate(subtitle):
        a(f'<text class="t2" x="{PAD}" y="{56 + 18 * k}" font-size="13">{escape(ln)}</text>')
    # Legend: one row above the plot, swatch + text in text ink.
    lx = PLOT_L
    for k, day in enumerate(DAYS):
        a(f'<circle class="d{k + 1}" cx="{f(lx + 5)}" cy="{legend_y}" r="5"/>')
        a(f'<text class="t2" x="{f(lx + 15)}" y="{f(legend_y + 4.5)}" font-size="12.5">{day}</text>')
        lx += 104
    # Grid, ticks and the zero line.
    for t in range(x_min, x_max + 1, step):
        x = f(x_of(t))
        if t != 0:
            a(f'<line class="grid" x1="{x}" y1="{plot_top - 4}" x2="{x}" y2="{plot_bot}"/>')
        a(
            f'<text class="mu" x="{x}" y="{plot_bot + 17}" font-size="12" text-anchor="middle">'
            f"{'0' if t == 0 else f'{t:+d}'}</text>"
        )
    zx = f(x_of(0))
    a(f'<line class="zero" x1="{zx}" y1="{plot_top - 4}" x2="{zx}" y2="{plot_bot}"/>')
    a(f'<text class="mu" x="{f(x_of(x_min))}" y="{plot_bot + 35}" font-size="12">underfills (pessimistic)</text>')
    a(
        f'<text class="mu" x="{f(x_of(x_max))}" y="{plot_bot + 35}" font-size="12" text-anchor="end">'
        "overfills (optimistic)</text>"
    )
    a(
        f'<text class="t2" x="{zx}" y="{plot_bot + 35}" font-size="12" text-anchor="middle" '
        'font-weight="600">exact = 0</text>'
    )
    top_model = rows[0]
    for i, r in enumerate(rows):
        cy = plot_top + ROW_H * i + ROW_H / 2
        a(
            f'<text class="t1 mono" x="{PLOT_L - 12}" y="{f(cy + 4)}" font-size="12.5" text-anchor="end">'
            f"{escape(label(r.model, r.model in marked))}</text>"
        )
        for k, e in enumerate(r.days):
            y = cy - 4 if k == 0 else cy + 4
            tip = f"{short_id(r.model)}, {DAYS[k]}: {e.value:+.2f} pp [{e.lo:+.2f}, {e.hi:+.2f}]"
            if r.model in marked and pop is not None and chart.aborts is not None:
                tip += f", n = {n(pop - chart.aborts.left_out[r.model][k])}"
            a(f"<g><title>{escape(tip)}</title>")
            a(f'<line class="ci c{k + 1}" x1="{f(x_of(e.lo))}" y1="{f(y)}" x2="{f(x_of(e.hi))}" y2="{f(y)}"/>')
            a(f'<circle class="d{k + 1} ring" cx="{f(x_of(e.value))}" cy="{f(y)}" r="4.5"/>')
            a("</g>")
        if r is top_model:
            lo = min(e.lo for e in r.days)
            a(
                f'<text class="t1" x="{f(x_of(lo) - 8)}" y="{f(cy + 4)}" font-size="12.5" font-weight="600" '
                'text-anchor="end">'
                f"{r.days[0].value:+.2f} / {r.days[1].value:+.2f} pp</text>"
            )
    y0 = h - 14 - 16 * (len(note_lines) - 1)
    for k, note in enumerate(note_lines):
        a(f'<text class="mu" x="{PAD}" y="{y0 + 16 * k}" font-size="11.5">{escape(note)}</text>')
    a("</svg>")
    return "\n".join(out) + "\n"


# A frozen copy of the section's shape (not its numbers), so the self-test does not depend on the published values.
FIXTURE = """\
# Results

## 4. How far are L2 queue models from exact truth? (fixture)

Text before the table.

**faithful mode** (fill-rate bias in pp, 95% CI)

| model | 2019-12-30 1 s | 2019-12-30 60 s | 2020-01-30 1 s |
|---|---|---|---|
| `alpha_model[k=1;particles=100]` | -0.06 [-0.33, +0.18] | +0.10 [+0.00, +0.20] | -0.32 [-0.77, +0.09] |
| `front_of_queue` | +1.00 [+0.50, +1.50] | +1.00 [+0.50, +1.50] | +1.00 [+0.50, +1.50] |
| `hbt_beta` | -0.23 [-0.52, +0.06] | +0.10 [+0.00, +0.20] | -0.50 [-0.88, -0.14] |
| `hbt_l3_fifo` | -1.30 [-1.71, -0.84] | +0.10 [+0.00, +0.20] | -2.17 [-2.85, -1.55] |
| `gamma_touch[p=1]` | +3.13 [+2.63, +3.64] | +0.10 [+0.00, +0.20] | +2.80 [+2.15, +3.55] |

**corrected mode** (fill-rate bias in pp, 95% CI)

| model | 2019-12-30 1 s | 2020-01-30 1 s |
|---|---|---|
| `alpha_model[k=1;particles=100]` | +9.00 [+8.00, +10.00] | +9.00 [+8.00, +10.00] |

**hftbacktest #312 aborts** (faithful mode, shadows of 3,000 left out of the model's metrics)

| model | 2019-12-30 | 2020-01-30 |
|---|---|---|
| `hbt_beta` | 179 | 232 |

## 5. Next section

**faithful mode** (fill-rate bias in pp, 95% CI)
"""


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise SelfTestError(msg)


def _fails(text: str, needle: str) -> None:
    try:
        parse_results(text, "fixture")
    except ChartError as e:
        _check(needle in str(e), f"expected an error containing {needle!r}, got {e}")
        return
    raise SelfTestError(f"expected a ChartError containing {needle!r}")


def _edit(text: str, old: str, new: str) -> str:
    _check(text.count(old) >= 1, f"self-test edit target {old!r} not in the fixture")
    return text.replace(old, new, 1)


def self_test(results: Path = RESULTS) -> None:
    """Parser and renderer behaviour on the frozen FIXTURE, then structural invariants of the live RESULTS.md."""
    chart = parse_results(FIXTURE, "fixture")
    ids = [r.model for r in chart.rows]
    _check(ids == ["alpha_model[k=1;particles=100]", "hbt_beta", "hbt_l3_fifo", "gamma_touch[p=1]"], f"rows {ids}")
    _check(chart.rows[3].days == (Est(3.13, 2.63, 3.64), Est(2.80, 2.15, 3.55)), "1 s cells of both days")
    _check(chart.aborts == Aborts(3000, {"hbt_beta": (179, 232)}), f"aborts {chart.aborts}")
    svg = render(chart)
    _check(svg == render(Chart(list(reversed(chart.rows)), chart.aborts)), "render must not depend on table order")
    _check(svg.count('class="t1 mono"') == 4 and svg.count('<circle class="d1 ring"') == 4, "one mark per row")
    _check(">alpha_model[k=1]<" in svg and "particles" not in svg, "default keys left out of the ids")
    _check(f">hbt_beta {MARK}<" in svg and ">hbt_l3_fifo (L3 input)<" in svg, "row labels")
    _check("+3.13 / +2.80 pp" in svg, "top model annotated")
    _check(
        "rests on 2,821 of 3,000 orders on 2019-12-30 and 2,768 on 2020-01-30" in " ".join(footnote(chart)),
        "footnote n from the table",
    )
    sub = re.findall(r'font-size="13">([^<]*)<', svg)
    foot = re.findall(r'font-size="11.5">([^<]*)<', svg)
    _check(len(sub) >= 2 and all(len(t) <= SUB_WRAP for t in sub), f"subtitle wrapped: {sub}")
    joined = " ".join(sub)
    _check(
        f"3,000 small orders per day ({MARK} rows: fewer)" in joined and "day-block bootstrap 95% CI" in joined,
        f"subtitle: {joined}",
    )
    _check(len(foot) >= 3 and all(len(t) <= NOTE_WRAP for t in foot), f"footnote wrapped: {foot}")
    _check(">-4</text>" in svg and ">+4</text>" in svg and ">+5</text>" not in svg, "default axis -4 .. +4")
    # A range of kept-order counts across several marked rows.
    two = _edit(FIXTURE, "| `hbt_beta` | 179 | 232 |", "| `hbt_beta` | 179 | 232 |\n| `hbt_l3_fifo` | 323 | 217 |")
    notes2 = " ".join(footnote(parse_results(two, "fixture")))
    _check("2,677 to 2,821 of 3,000 orders on 2019-12-30 and 2,768 to 2,783 on 2020-01-30" in notes2, "footnote ranges")
    # Without the per-model rule (a common-population rerun), no row is marked and no #312 note is drawn.
    common = _edit(FIXTURE, "shadows of 3,000 left out of the model's metrics", "common population")
    chart3 = parse_results(common, "fixture")
    svg3 = render(chart3)
    _check(chart3.aborts is None and MARK not in svg3 and "#312" not in svg3, "no note without the per-model rule")
    no_aborts = parse_results(FIXTURE.split(ABORTS_TITLE)[0] + "## 5. Next\n", "fixture")
    # hbt_power[n=1] (equal to hbt_power3[n=1]) is named in the footnote only when the table has the row.
    _check("hbt_power[n=1]" not in " ".join(footnote(chart)), "footnote names a row the table lacks")
    dup = _edit(
        FIXTURE,
        "| `hbt_l3_fifo` | -1.30",
        "| `hbt_power[n=1]` | +0.07 [-0.26, +0.37] | +0.10 [+0.00, +0.20] | -0.47 [-0.84, -0.10] |\n"
        "| `hbt_l3_fifo` | -1.30",
    )
    chart5 = parse_results(dup, "fixture")
    _check(chart5.rows == chart.rows and chart5.skipped == ("front_of_queue", "hbt_power[n=1]"), "skipped rows")
    _check("hbt_power[n=1], which equals hbt_power3[n=1]" in " ".join(footnote(chart5)), "footnote names the row")
    _check(no_aborts.aborts is None, "abort table is optional")
    # A value beyond +-4 pp widens the axis; it is never clamped to the plot edge.
    wide = _edit(FIXTURE, "| `hbt_beta` | -0.23 [-0.52, +0.06]", "| `hbt_beta` | -9.23 [-9.52, -8.06]")
    svg4 = render(parse_results(wide, "fixture"))
    _check(">-10</text>" in svg4 and ">+10</text>" in svg4 and ">-9</text>" not in svg4, "axis widened, step 2")
    _check(axis([Row("x", (Est(0, -13.2, 1), Est(0, 0, 0)))]) == (15, 5), "axis step 5 beyond 12 pp")
    # Edits to a left-out row do not reach the chart; edits to other sections or tables do not either.
    _check(
        parse_results(_edit(FIXTURE, "| `front_of_queue` | +1.00 [+0.50, +1.50]", "| `front_of_queue` | x")) == chart,
        "left-out rows ignored",
    )
    _check(
        parse_results(_edit(FIXTURE, "| `alpha_model[k=1;particles=100]` | +9.00", "| `zz` | +9.00")) == chart,
        "corrected table ignored",
    )
    # Failure modes, each with a message naming the problem.
    _fails(_edit(FIXTURE, SECTION, "## 4. Renamed section"), "not found")
    _fails(_edit(FIXTURE, TABLE_TITLE, "**faithful**"), "not found")
    _fails(_edit(FIXTURE, "| 2020-01-30 1 s |", "| 2020-01-30 2 s |"), "lacks column(s) '2020-01-30 1 s'")
    _fails(_edit(FIXTURE, "| model | 2019-12-30 1 s |", "| model | 2019-12-30 2 s |"), "'2019-12-30 1 s'")
    _fails(
        _edit(FIXTURE, "| `gamma_touch[p=1]` | +3.13 [+2.63, +3.64]", "| `gamma_touch[p=1]` | +3.13 (+2.63, +3.64)"),
        "cannot parse",
    )
    _fails(
        _edit(FIXTURE, "| `gamma_touch[p=1]` | +3.13 [+2.63, +3.64]", "| `gamma_touch[p=1]` | +4.13 [+2.63, +3.64]"),
        "outside its CI",
    )
    _fails(
        _edit(
            FIXTURE,
            "| `hbt_beta` | -0.23 [-0.52, +0.06] | +0.10 [+0.00, +0.20] | -0.50 [-0.88, -0.14] |",
            "| `hbt_beta` |",
        ),
        "1 cells where the header has 4",
    )
    _fails(_edit(FIXTURE, "| `hbt_l3_fifo` |", "| `hbt_beta` |"), "appears twice")
    head = FIXTURE[: FIXTURE.index(TABLE_TITLE) + len(TABLE_TITLE)]
    _fails(head + "\n\nno table here\n", "no table")
    table_head = FIXTURE[: FIXTURE.index("| `alpha_model")]
    _fails(
        table_head + "| `gamma_touch[p=1]` | +3.13 [+2.63, +3.64] | +0.10 [+0.00, +0.20] | +2.80 [+2.15, +3.55] |\n",
        "fewer than 2",
    )
    _fails(_edit(FIXTURE, "| `hbt_beta` | 179 |", "| `hbt_beta` | many |"), "as a count")
    _fails(_edit(FIXTURE, "| `hbt_beta` | 179 |", "| `hbt_beta` | 3,001 |"), "exceed the population")
    _fails(_edit(FIXTURE, "| `hbt_beta` | 179 |", "| `hbt_gone` | 179 |"), "not in the faithful table: hbt_gone")
    _fails(
        _edit(FIXTURE, "| model | 2019-12-30 | 2020-01-30 |", "| model | 2019-12-30 | day two |"),
        "abort table header lacks column(s) 2020-01-30",
    )
    # The live file: only what --check relies on, never its published values.
    live = parse_results(results.read_text(encoding="utf-8"), results.name)
    live_ids = [r.model for r in live.rows]
    _check(len(live.rows) >= 2 and len(set(live_ids)) == len(live_ids), f"{results.name}: rows {live_ids}")
    _check(not set(LEFT_OUT) & set(live_ids), f"{results.name}: a left-out row was charted")
    _check(render(live) == render(Chart(list(reversed(live.rows)), live.aborts)), "live render order-independent")
    print("self-test ok")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the SVG differs from a fresh render")
    ap.add_argument("--results", type=Path, default=RESULTS, help="RESULTS.md to read (default: docs/RESULTS.md)")
    ap.add_argument("--out", type=Path, default=OUT, help="SVG to write or check")
    ap.add_argument("--self-test", action="store_true", help="run the parser and renderer checks and exit")
    a = ap.parse_args(argv)
    try:
        text = a.results.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"QT-INPUT-001: cannot read {a.results}: {e}", file=sys.stderr)
        return 2
    if a.self_test:
        try:
            self_test(a.results)
        except (SelfTestError, ChartError) as e:
            print(f"self-test failed: {e}", file=sys.stderr)
            return 1
        return 0
    try:
        svg = render(parse_results(text, a.results.name))
    except ChartError as e:
        print(f"QT-INPUT-002: {e}", file=sys.stderr)
        return 2
    if a.check:
        if not a.out.exists() or a.out.read_text(encoding="utf-8") != svg:
            print(f"{a.out} is stale; run `python docs/assets/readme_chart.py`", file=sys.stderr)
            return 1
        print(f"{a.out} is current")
        return 0
    a.out.write_text(svg, encoding="utf-8")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
