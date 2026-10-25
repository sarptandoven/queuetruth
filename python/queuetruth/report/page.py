"""HTML building blocks and the page shell: stylesheet (light and dark tokens via `prefers-color-scheme`, a theme
switch that needs no JavaScript, print rules that always use the light tokens and always print the header's
provenance, Okabe-Ito-derived color-blind-safe series slots, status colors always paired with an icon and a label),
tables (optionally with row headers), collapsible `<details>` blocks, figures with legends and `<details>` data-table
fallbacks, callouts, code blocks and the optional copy-button script (the page is complete without it).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from queuetruth.report.fmt import esc


class Raw(str):
    """Pre-rendered HTML that must not be escaped again."""


def cell(v: object) -> str:
    """Escapes plain values; passes `Raw` through."""
    return v if isinstance(v, Raw) else esc(v)


def table(
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
    *,
    numeric: Iterable[int] = (),
    cls: str = "",
    row_cls: Sequence[str] | None = None,
    caption: str = "",
    row_header: bool = False,
) -> Raw:
    """A data table; columns listed in `numeric` are right-aligned with tabular figures. With `row_header` the first
    cell of each body row is a `<th scope="row">` (screen readers announce it with every cell of the row)."""
    num_cols = set(numeric)
    out = [f'<div class="tw"><table class="{esc(cls)}">' if cls else '<div class="tw"><table>']
    if caption:
        out.append(f"<caption>{esc(caption)}</caption>")
    out.append("<thead><tr>")
    for i, h in enumerate(headers):
        out.append(f'<th scope="col" class="n">{cell(h)}</th>' if i in num_cols else f'<th scope="col">{cell(h)}</th>')
    out.append("</tr></thead><tbody>")
    for ri, r in enumerate(rows):
        rc = row_cls[ri] if row_cls and ri < len(row_cls) and row_cls[ri] else ""
        out.append(f'<tr class="{rc}">' if rc else "<tr>")
        for i, v in enumerate(r):
            if i == 0 and row_header:
                out.append(f'<th scope="row">{cell(v)}</th>')
            else:
                out.append(f'<td class="n">{cell(v)}</td>' if i in num_cols else f"<td>{cell(v)}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return Raw("".join(out))


def kv(pairs: Iterable[tuple[str, object]], cls: str = "kv") -> Raw:
    """Definition list of label / value pairs."""
    out = [f'<dl class="{cls}">']
    for k, v in pairs:
        out.append(f"<div><dt>{cell(k)}</dt><dd>{cell(v)}</dd></div>")
    out.append("</dl>")
    return Raw("".join(out))


def legend(items: Sequence[tuple[str, int, bool]]) -> Raw:
    """Legend swatches (name, slot, dashed); omitted for a single series."""
    if len(items) < 2:
        return Raw("")
    parts = ['<ul class="legend">']
    for name, slot, dashed in items:
        dash = " dash" if dashed else ""
        parts.append(f'<li><span class="sw s{slot}{dash}" aria-hidden="true"></span>{esc(name)}</li>')
    parts.append("</ul>")
    return Raw("".join(parts))


def figure(
    svg: str, caption: str, *, items: Sequence[tuple[str, int, bool]] = (), data: str = "", note: str = ""
) -> Raw:
    """Chart figure: legend, inline SVG, caption, optional note and the data table in `<details>`."""
    out = ['<figure class="fig">', legend(items), f'<div class="svgw">{svg}</div>', f"<figcaption>{esc(caption)}"]
    if note:
        out.append(f' <span class="note">{esc(note)}</span>')
    out.append("</figcaption>")
    if data:
        out.append(f"<details><summary>Data table</summary>{data}</details>")
    out.append("</figure>")
    return Raw("".join(out))


@dataclass
class Status:
    """Status styling: CSS class, icon and label (color never carries meaning alone)."""

    cls: str
    icon: str
    label: str


GOOD = Status("good", "✓", "PASS")
WARN = Status("warn", "!", "WARNING")
CRIT = Status("crit", "✕", "FAIL")
INFO = Status("info", "i", "NOTE")
#: Grey: nothing to judge (e.g. no scorecard gates configured; review F3).
NEUTRAL = Status("muted", "–", "NONE")


def badge(st: Status, label: str | None = None) -> Raw:
    """Inline status badge with icon and text."""
    return Raw(
        f'<span class="badge {st.cls}"><span aria-hidden="true">{esc(st.icon)}</span> {esc(label or st.label)}</span>'
    )


def callout(st: Status, title: str, body: str = "") -> Raw:
    """Boxed message with a status icon, a bold title and optional HTML body."""
    return Raw(
        f'<div class="callout {st.cls}" role="note"><span class="ico" aria-hidden="true">{esc(st.icon)}</span>'
        f"<div><strong>{esc(title)}</strong>{body}</div></div>"
    )


def code_block(text: str, label: str = "") -> Raw:
    """Preformatted code; the optional script adds a copy button."""
    head = f'<div class="codehead">{esc(label)}</div>' if label else ""
    return Raw(f'<div class="codewrap">{head}<pre class="code"><code>{esc(text)}</code></pre></div>')


def ul(items: Iterable[object], cls: str = "") -> Raw:
    """Bulleted list."""
    li = "".join(f"<li>{cell(i)}</li>" for i in items)
    return Raw(f'<ul class="{cls}">{li}</ul>' if cls else f"<ul>{li}</ul>")


def section(sid: str, title: str, body: Sequence[str], lead: str = "", cls: str = "") -> Raw:
    """A top-level report section with an anchor id; the heading labels the region for assistive technology."""
    lead_html = f'<p class="lead">{lead}</p>' if lead else ""
    c = f' class="{esc(cls)}"' if cls else ""
    return Raw(
        f'<section id="{esc(sid)}"{c} aria-labelledby="{esc(sid)}-h"><h2 id="{esc(sid)}-h">{esc(title)}</h2>'
        f"{lead_html}{''.join(body)}</section>"
    )


def details(
    summary: str,
    body: Sequence[str] | str,
    *,
    note: str = "",
    cls: str = "more",
    sid: str = "",
    is_open: bool = False,
    summary_html: str = "",
) -> Raw:
    """A collapsible block, closed unless `is_open` (so the page stays short and prints only what the reader opened,
    except blocks with class `prov`, which always print):
    `summary` is plain text (or `summary_html`, pre-rendered), `note` a muted plain-text suffix, `body` pre-rendered
    HTML."""
    inner = body if isinstance(body, str) else "".join(body)
    n = f' <span class="note">{esc(note)}</span>' if note else ""
    i = f' id="{esc(sid)}"' if sid else ""
    c = f' class="{esc(cls)}"' if cls else ""
    o = " open" if is_open else ""
    head = summary_html or esc(summary)
    return Raw(f"<details{c}{i}{o}><summary>{head}{n}</summary>{inner}</details>")


def theme_switch() -> Raw:
    """Auto / light / dark switch: radio inputs the stylesheet reads with `:has()`; no script, nothing persisted.
    Auto follows `prefers-color-scheme`; print always uses the light theme."""
    parts = ['<fieldset class="theme"><legend>Color theme</legend>']
    for key, label in (("auto", "Auto"), ("light", "Light"), ("dark", "Dark")):
        rid = THEME_IDS[key]
        checked = " checked" if key == "auto" else ""
        parts.append(
            f'<input type="radio" name="qt-theme" id="{rid}" value="{key}"{checked}><label for="{rid}">{label}</label>'
        )
    parts.append("</fieldset>")
    return Raw("".join(parts))


#: Color tokens. Light is the base; dark applies under `prefers-color-scheme: dark` on screen, and either can be
#: forced with the header's theme switch (radio inputs read by `:has()`, no JavaScript) or `data-theme` on <html>.
#: Print always uses the light tokens.
LIGHT_TOKENS = (
    "--bg:#f7f6f3;--surface:#ffffff;--surface-2:#f1f0ec;--border:#e1dfd9;--text:#0b0b0b;--text-2:#52514e;"
    "--muted:#6a6863;--grid:#e7e5e0;--axis:#8a8883;--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#4a3aa7;"
    "--div-neg:#2a78d6;--div-pos:#e34948;--div-mid:#f0efec;--good:#0c7a0c;--good-bg:#e8f5e8;--warn:#8a5a00;"
    "--warn-bg:#fdf3dc;--crit:#b42c2c;--crit-bg:#fbe9e9;--info:#1c5cab;--info-bg:#e9f1fc;--code-bg:#f4f3ef;"
    "--band:#2a78d6;--focus:#1c5cab"
)
DARK_TOKENS = (
    "--bg:#121211;--surface:#1a1a19;--surface-2:#222220;--border:#34332f;--text:#f4f3ef;--text-2:#c3c2b7;"
    "--muted:#9a988f;--grid:#2c2b28;--axis:#6f6d68;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#9085e9;"
    "--div-neg:#3987e5;--div-pos:#e66767;--div-mid:#383835;--good:#5fd35f;--good-bg:#173017;--warn:#fab219;"
    "--warn-bg:#33270c;--crit:#ff8585;--crit-bg:#3a1a1a;--info:#86b6ef;--info-bg:#16263d;--code-bg:#161615;"
    "--band:#3987e5;--focus:#86b6ef"
)

#: Ids of the theme switch radios (`theme_switch`); the stylesheet keys the forced themes on them.
THEME_IDS = {"auto": "qt-theme-auto", "light": "qt-theme-light", "dark": "qt-theme-dark"}

# Each forced theme is its own rule: a browser without `:has()` drops only that rule and keeps the automatic theme.
CSS = (
    f":root{{color-scheme:light dark;{LIGHT_TOKENS}}}\n"
    f"@media screen and (prefers-color-scheme: dark){{:root{{color-scheme:dark;{DARK_TOKENS}}}}}\n"
    "@media screen{"
    f':root[data-theme="light"]{{color-scheme:light;{LIGHT_TOKENS}}}'
    f':root[data-theme="dark"]{{color-scheme:dark;{DARK_TOKENS}}}'
    f":root:has(#{THEME_IDS['light']}:checked){{color-scheme:light;{LIGHT_TOKENS}}}"
    f":root:has(#{THEME_IDS['dark']}:checked){{color-scheme:dark;{DARK_TOKENS}}}"
    "}\n"
    + """*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
main{max-width:1180px;margin:0 auto;padding:16px 16px 48px}
h1{font-size:24px;line-height:1.2;margin:0 0 2px;letter-spacing:-.01em}
h2{font-size:20px;margin:0 0 10px;padding-top:2px}
h3{font-size:16px;margin:20px 0 8px}
h4{font-size:14px;margin:16px 0 6px;color:var(--text-2)}
p{margin:6px 0 10px}
a{color:var(--info)}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
code,pre,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
.skip{position:absolute;left:-9999px;top:0}
.skip:focus{left:16px;top:8px;z-index:20;background:var(--surface);padding:6px 10px;border:1px solid var(--border);
border-radius:6px}
header.top{display:flex;flex-wrap:wrap;align-items:flex-start;justify-content:space-between;gap:8px 16px}
header.top>.ttl{min-width:0;flex:1 1 420px}
header.top>.wide{flex:1 1 100%;min-width:0}
.sub{color:var(--text-2);margin:0 0 10px}
section,header#header,details[id]{scroll-margin-top:52px}
section{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:18px 20px;margin:16px 0}
section.answer{border-width:2px;border-color:var(--text-2)}
.lead{color:var(--text-2);max-width:900px}
nav.toc{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;gap:4px 14px;margin:8px -16px 0;
padding:8px 16px;font-size:13px;background:var(--bg);border-bottom:1px solid var(--border)}
nav.toc a{text-decoration:none}
nav.toc a:hover{text-decoration:underline}
fieldset.theme{display:inline-flex;align-items:center;gap:2px;border:1px solid var(--border);border-radius:999px;
padding:2px;margin:4px 0 0;font-size:12px}
fieldset.theme legend{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
fieldset.theme input{position:absolute;opacity:0;width:1px;height:1px;margin:0}
fieldset.theme label{padding:2px 10px;border-radius:999px;cursor:pointer;color:var(--text-2)}
fieldset.theme input:checked+label{background:var(--text);color:var(--bg);font-weight:600}
fieldset.theme input:focus-visible+label{outline:2px solid var(--focus);outline-offset:1px}
.tw{overflow-x:auto;margin:8px 0 12px}
table{border-collapse:collapse;font-size:13px;min-width:50%}
caption{text-align:left;color:var(--text-2);padding:0 0 4px;font-size:12.5px;caption-side:top}
th,td{padding:4px 10px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}
thead th{font-weight:600;color:var(--text-2);background:var(--surface-2);white-space:nowrap}
tbody th{font-weight:500;text-align:left}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tr.hl td,tr.hl th{background:var(--warn-bg)}
tr.fill td{background:var(--good-bg)}
tr.base td,tr.base th{background:var(--surface-2);font-weight:600}
tr.gap td{color:var(--muted);text-align:center}
table.why td{white-space:nowrap}
table.lb tbody th{min-width:18ch}
table.files td:first-child{min-width:22ch;overflow-wrap:anywhere}
table.files .mono{font-size:11px;word-break:break-all;display:inline-block;min-width:24ch}
h2,h3,h4,p,li,figcaption,summary{overflow-wrap:anywhere}
td.hash{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11.5px;word-break:break-all}
dl.kv{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:6px 18px;margin:8px 0 12px}
dl.kv div{border-bottom:1px solid var(--border);padding:3px 0}
dl.kv dt{color:var(--text-2);font-size:12px;overflow-wrap:anywhere}
dl.kv dd{margin:0;font-variant-numeric:tabular-nums;font-weight:500;overflow-wrap:anywhere}
.fig{margin:12px 0 18px}
.svgw{overflow-x:auto}
.svgw svg{max-width:100%;height:auto;display:block}
figcaption{color:var(--text-2);font-size:12.5px;margin-top:4px;max-width:900px}
.note{color:var(--muted)}
details{margin:6px 0}
details>summary{cursor:pointer;color:var(--text-2);font-size:13px}
details>summary:hover{color:var(--text)}
details.more{border:1px solid var(--border);border-radius:8px;padding:6px 12px;margin:10px 0;background:var(--surface)}
details.more[open]{padding-bottom:10px}
details.more>summary{font-weight:600;color:var(--text);font-size:13.5px;padding:2px 0}
details.more>summary .note{font-weight:400}
summary>h2,summary>h3,summary>h4{display:inline;font-size:inherit;margin:0;padding:0;color:inherit}
details.item{border-top:1px solid var(--border);margin:0;padding:4px 0}
details.item>summary{color:var(--text);font-size:13.5px;padding:2px 0}
details.item>summary h3,details.item>summary h4{display:inline;font-size:14px;margin:0;color:var(--text)}
details.item[open]>summary{margin-bottom:6px}
details.item+:not(details.item){margin-top:12px}
ol.answers{list-style:none;margin:6px 0 4px;padding:0;display:grid;gap:10px}
ol.answers>li{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;align-items:start;border:1px solid var(--border);
border-radius:8px;padding:10px 12px;background:var(--surface-2)}
ol.answers h3{margin:0;font-size:14.5px}
ol.answers p{margin:2px 0 0;font-size:15px;line-height:1.45}
ol.answers .go{font-size:12.5px}
ol.answers>li>.badge{margin-top:1px}
ul.legend{list-style:none;display:flex;flex-wrap:wrap;gap:4px 16px;padding:0;margin:0 0 4px;font-size:12.5px;
color:var(--text-2)}
.sw{display:inline-block;width:16px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
.sw.s1{background:var(--s1)}.sw.s2{background:var(--s2)}.sw.s3{background:var(--s3)}.sw.s4{background:var(--s4)}
.sw.dash{background:repeating-linear-gradient(90deg,currentColor 0 4px,transparent 4px 7px);color:var(--s2)}
.callout{display:flex;gap:10px;border-radius:8px;padding:10px 12px;margin:10px 0;border:1px solid var(--border)}
.callout .ico{flex:none;width:22px;height:22px;border-radius:50%;display:inline-flex;align-items:center;
justify-content:center;font-weight:700;font-size:13px;color:var(--surface)}
.callout.good{background:var(--good-bg)}.callout.good .ico{background:var(--good)}
.callout.warn{background:var(--warn-bg)}.callout.warn .ico{background:var(--warn)}
.callout.crit{background:var(--crit-bg)}.callout.crit .ico{background:var(--crit)}
.callout.info{background:var(--info-bg)}.callout.info .ico{background:var(--info)}
.callout ul{margin:4px 0 0;padding-left:18px}
.callout p{margin:2px 0 4px}
.callout>div{min-width:0;flex:1}
.grid2>div{min-width:0}
.badge{display:inline-block;white-space:nowrap;border-radius:999px;padding:1px 9px;font-size:12px;font-weight:600;
border:1px solid}
.badge.good{color:var(--good);border-color:var(--good)}.badge.warn{color:var(--warn);border-color:var(--warn)}
.badge.crit{color:var(--crit);border-color:var(--crit)}.badge.info{color:var(--info);border-color:var(--info)}
.badge.muted{color:var(--muted);border-color:var(--muted)}
.verdict{border:1px solid var(--border);border-radius:10px;padding:16px 18px;margin:10px 0;background:var(--surface-2)}
.verdict .kicker{color:var(--text-2);font-size:12.5px;overflow-wrap:anywhere}
.verdict .headline{font-size:19px;font-weight:600;margin:6px 0 12px;line-height:1.35}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:10px 12px}
.tile .t{color:var(--text-2);font-size:12px}
.tile .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.25}
.tile .s{color:var(--muted);font-size:12px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,460px),1fr));gap:4px 20px}
.relgrid{display:flex;flex-wrap:wrap;gap:6px 10px;margin:4px 0 10px}
.relgrid svg{width:190px;height:auto}
.codewrap{position:relative;margin:8px 0 12px}
.codehead{font-size:12px;color:var(--text-2);margin-bottom:2px}
pre.code{background:var(--code-bg);border:1px solid var(--border);border-radius:8px;padding:10px 12px;overflow-x:auto;
margin:0;white-space:pre}
button.copy{position:absolute;top:22px;right:8px;font:12px system-ui,sans-serif;padding:2px 8px;border-radius:6px;
border:1px solid var(--border);background:var(--surface);color:var(--text-2);cursor:pointer}
footer{color:var(--muted);font-size:12px;margin-top:24px}
svg .grid{stroke:var(--grid);stroke-width:1}
svg .axis{stroke:var(--axis);stroke-width:1}
svg .zero{stroke:var(--axis);stroke-width:1.2}
svg .ref{stroke:var(--axis);stroke-width:1.2;stroke-dasharray:5 4}
svg .ref.est{stroke:var(--text);stroke-dasharray:none}
svg .diag{stroke:var(--axis);stroke-width:1.2;stroke-dasharray:4 4}
svg .xband{fill:var(--s3);fill-opacity:.12}
svg text{font:11px system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;fill:var(--text-2)}
svg text.lab{fill:var(--text-2);font-size:11.5px}
svg text.reflab{fill:var(--muted);font-size:10.5px}
svg text.rowlab{fill:var(--text);font-size:11px}
svg text.ptitle{fill:var(--text);font-size:11px;font-weight:600}
svg text.cell{fill:var(--text);font-size:10.5px}
svg text.endlab{font-size:11px;font-weight:600;fill:var(--text)}
svg .ln{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
svg .ln.dash{stroke-dasharray:6 4}
svg .ln.s1,svg .wh.s1{stroke:var(--s1)}svg .ln.s2,svg .wh.s2{stroke:var(--s2)}
svg .ln.s3,svg .wh.s3{stroke:var(--s3)}svg .ln.s4,svg .wh.s4{stroke:var(--s4)}
svg .wh{stroke-width:1.6;stroke-linecap:round}
svg .band{stroke:none;fill-opacity:.16}
svg .band.s1,svg .mk.s1,svg .bar.s1{fill:var(--s1)}svg .band.s2,svg .mk.s2,svg .bar.s2{fill:var(--s2)}
svg .band.s3,svg .mk.s3,svg .bar.s3{fill:var(--s3)}svg .band.s4,svg .mk.s4,svg .bar.s4{fill:var(--s4)}
svg .mk{stroke:var(--surface);stroke-width:1.5}
svg .cnt{fill:var(--axis);fill-opacity:.45}
svg .hm0{fill:var(--div-mid)}
svg .hmp{fill:var(--div-pos)}
svg .hmn{fill:var(--div-neg)}
@media (max-width:640px){section{padding:14px 12px}ol.answers>li{grid-template-columns:1fr}}
@media (max-width:760px){nav.toc{flex-wrap:nowrap;overflow-x:auto;white-space:nowrap;gap:4px 12px;
scrollbar-width:thin}}
@media (prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
@page{margin:12mm}
@media print{body{background:#fff;font-size:10.5pt}main{max-width:none;padding:0}
nav.toc,fieldset.theme,button.copy,.skip{display:none}
section{border:0;border-top:1px solid #999;border-radius:0;padding:10px 0;margin:10px 0;break-inside:auto}
section.answer{border-top:2px solid #000}
h2,h3,h4,summary{break-after:avoid}
figure,.callout,.tile,ol.answers>li,tr,dl.kv div{break-inside:avoid}
.tw,.svgw{overflow:visible}table{width:100%;font-size:8.5pt}thead th{white-space:normal}
details.more,details.item{border:0;padding:0}
details.prov::details-content{content-visibility:visible;display:contents}
@supports not selector(::details-content){.print-prov[hidden]{display:block}}
*{-webkit-print-color-adjust:exact;print-color-adjust:exact}
a{color:inherit;text-decoration:none}}
"""
)

SCRIPT = (
    "(function(){if(!navigator.clipboard)return;document.querySelectorAll('pre.code').forEach(function(p){"
    "var b=document.createElement('button');b.className='copy';b.type='button';b.textContent='Copy';"
    "b.addEventListener('click',function(){navigator.clipboard.writeText(p.innerText).then(function(){"
    "b.textContent='Copied';setTimeout(function(){b.textContent='Copy'},1500)})});p.parentNode.appendChild(b)})})();"
)


def shell(title: str, body: str, description: str, *, skip_to: str = "") -> str:
    """The complete HTML document; `skip_to` (an element id) adds a "skip to" link as the first focusable element."""
    skip = f'<a class="skip" href="#{esc(skip_to)}">Skip to the answer</a>\n' if skip_to else ""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f'<meta name="description" content="{esc(description)}">\n'
        '<meta name="generator" content="queuetruth">\n'
        f"<title>{esc(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n{skip}<main>\n"
        f"{body}\n</main>\n<script>{SCRIPT}</script>\n</body>\n</html>\n"
    )
