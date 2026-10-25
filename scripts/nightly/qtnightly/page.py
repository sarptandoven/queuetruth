"""Static HTML results page: the latest run (verdict, checks, gates, changes against the previous run) and the run
history. One self-contained file (inline CSS, no script, no external resource), light and dark."""

from __future__ import annotations

import html
from typing import Any

from . import EXIT_MEANING

CSS = """
:root{--bg:#ffffff;--fg:#1c1f23;--muted:#5f6b76;--line:#d9dee3;--card:#f6f8fa;--pass:#1a7f37;--pass-bg:#dafbe1;
--fail:#cf222e;--fail-bg:#ffebe9;--error:#9a6700;--error-bg:#fff8c5;--info:#0969da;--info-bg:#ddf4ff}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;
--line:#30363d;--card:#161b22;--pass:#3fb950;--pass-bg:#12261e;--fail:#f85149;--fail-bg:#2d1214;--error:#d29922;
--error-bg:#2b2111;--info:#4493f8;--info-bg:#0c2d6b}}
:root[data-theme="dark"]{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--line:#30363d;--card:#161b22;--pass:#3fb950;
--pass-bg:#12261e;--fail:#f85149;--fail-bg:#2d1214;--error:#d29922;--error-bg:#2b2111;--info:#4493f8;--info-bg:#0c2d6b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",
Helvetica,Arial,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}h3{font-size:15px;margin:0}
.muted{color:var(--muted)}code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.verdict{border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:16px 0;background:var(--card)}
.pill{display:inline-block;border-radius:999px;padding:1px 9px;font-size:12px;font-weight:600;white-space:nowrap}
.pass{color:var(--pass);background:var(--pass-bg)}.fail{color:var(--fail);background:var(--fail-bg)}
.error{color:var(--error);background:var(--error-bg)}.info{color:var(--info);background:var(--info-bg)}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600}td.num{text-align:right;font-variant-numeric:tabular-nums}
details{border:1px solid var(--line);border-radius:8px;margin:10px 0;background:var(--card)}
summary{cursor:pointer;padding:10px 12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
details>div{padding:0 12px 12px}
ul.gates{list-style:none;padding:0;margin:8px 0}ul.gates li{padding:2px 0}
a{color:var(--info)}
"""


def esc(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def pill(status: str | None) -> str:
    s = status or "none"
    cls = {"pass": "pass", "fail": "fail", "error": "error"}.get(s, "info")
    return f'<span class="pill {cls}">{esc(s)}</span>'


def fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.6g}"
    return esc(v)


def _changes_for(diff: dict[str, Any], check: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for kind in ("changed", "noisy"):
        for e in diff.get(kind, []):
            if e["check"] == check:
                out[e["metric"]] = {**e, "noisy": kind == "noisy"}
    return out


def render_check(name: str, c: dict[str, Any], diff: dict[str, Any], link_base: str) -> str:
    gates = c.get("gates", [])
    bad = [g for g in gates if not g.get("ok")]
    role = "required" if c.get("required", True) else "advisory"
    head = (
        f"<summary>{pill(c.get('status'))}<h3>{esc(c.get('title', name))}</h3>"
        f'<span class="muted">{esc(role)} &middot; {fmt(c.get("wall_s"))} s &middot; '
        f"{len(gates) - len(bad)}/{len(gates)} gates</span></summary>"
    )
    parts = [f'<details{" open" if c.get("status") != "pass" else ""} id="check-{esc(name)}">', head, "<div>"]
    if c.get("reason"):
        parts.append(f'<p class="mono">{esc(c["reason"])}</p>')
    if c.get("versions"):
        vs = ", ".join(f"{esc(k)} {esc(v)}" for k, v in sorted(c["versions"].items()))
        parts.append(f'<p class="muted">{vs}</p>')
    if c.get("provenance"):
        pv = "<br>".join(f"{esc(k)} {esc(v)}" for k, v in sorted(c["provenance"].items()))
        parts.append(f'<p class="muted mono">{pv}</p>')
    if gates:
        parts.append('<ul class="gates">')
        for g in gates:
            mark = pill("pass" if g.get("ok") else "fail")
            parts.append(f'<li>{mark} {esc(g["name"])} <span class="muted mono">{esc(g.get("detail"))}</span></li>')
        parts.append("</ul>")
    links = [f'<a href="{esc(link_base + p)}">{esc(k)}</a>' for k, p in sorted(c.get("artifacts", {}).items())]
    if links:
        parts.append("<p>" + " &middot; ".join(links) + "</p>")
    metrics = c.get("metrics", {})
    if metrics:
        ch = _changes_for(diff, name)
        parts.append('<div class="scroll"><table><tr><th>metric</th><th>value</th><th>previous</th></tr>')
        for k in sorted(metrics):
            e = ch.get(k)
            prev = ""
            if e is not None:
                tag = "noisy" if e["noisy"] else "changed"
                prev = f'{fmt(e.get("before"))} <span class="pill {"info" if e["noisy"] else "error"}">{tag}</span>'
            parts.append(
                f'<tr><td class="mono">{esc(k)}</td><td class="num">{fmt(metrics[k])}</td><td>{prev}</td></tr>'
            )
        parts.append("</table></div>")
    parts.append("</div></details>")
    return "".join(parts)


def _bench_trend(history: dict[str, Any], mode: str | None, max_rows: int) -> str:
    """Benchmark medians of the runs of `mode`, newest first: one column per case or derived ratio."""
    runs = [r for r in reversed(history.get("runs", [])) if r.get("mode") == mode]
    rows = [(r, r.get("checks", {}).get("bench", {}).get("metrics", {})) for r in runs[:max_rows]]
    rows = [(r, m) for r, m in rows if any(k.endswith(".median") for k in m)]
    if not rows:
        return ""
    cols = sorted({k for _, m in rows for k in m if k.endswith(".median")})
    head = "".join(f'<th class="mono">{esc(k.removeprefix("bench.").removesuffix(".median"))}</th>' for k in cols)
    out = [f'<h2>Benchmark medians</h2><div class="scroll"><table><tr><th>run</th><th>bench</th>{head}</tr>']
    for r, m in rows:
        st = r.get("checks", {}).get("bench", {}).get("status")
        cells = "".join(f'<td class="num">{fmt(m.get(k))}</td>' for k in cols)
        out.append(f'<tr><td class="mono">{esc(r.get("nightly_id"))}</td><td>{pill(st)}</td>{cells}</tr>')
    out.append("</table></div>")
    return "".join(out)


def render(
    run: dict[str, Any],
    history: dict[str, Any],
    link_base: str = "",
    max_history: int = 60,
    runs_base: str | None = None,
    run_pages: set[str] | None = None,
) -> str:
    """The page for `run` (a run summary) with the history table. `link_base` prefixes artifact links (relative to
    the run directory), e.g. `runs/<nightly_id>/` for the page at the state root. History rows whose id is in
    `run_pages` link to `<runs_base><nightly_id>/index.html`."""
    code = run.get("exit_code")
    verdict = run.get("verdict") or EXIT_MEANING.get(code, f"exit {code}")
    cls = "pass" if code == 0 else "fail" if code in (1, 2) else "error"
    git = run.get("git", {})
    diff = run.get("diff", {})
    checks = run.get("checks", {})
    out = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>queuetruth nightly</title>",
        f"<style>{CSS}</style></head><body><main>",
        "<h1>queuetruth nightly cross-checks</h1>",
        f'<p class="muted">run <code>{esc(run.get("nightly_id"))}</code> &middot; mode {esc(run.get("mode"))} &middot; '
        f"started {esc(run.get('started_at'))} &middot; {fmt(run.get('wall_s'))} s &middot; git "
        f"<code>{esc((git.get('head') or '')[:12])}</code>{' (dirty)' if git.get('dirty') else ''} "
        f"{esc(git.get('branch') or '')}</p>",
        f'<div class="verdict"><span class="pill {cls}">exit {esc(code)}</span> {esc(verdict)}',
    ]
    if run.get("flags"):
        out.append(f'<p class="muted">flags: {esc(", ".join(run["flags"]))}</p>')
    if run.get("runner_error"):
        out.append(f'<p class="mono">{esc(run["runner_error"])}</p>')
    out.append("</div>")

    out.append(
        '<h2>Checks</h2><div class="scroll"><table><tr><th>check</th><th>status</th><th>role</th>'
        "<th>gates</th><th>time (s)</th><th>previous</th></tr>"
    )
    prev_status = {s["check"]: s["before"] for s in diff.get("status_changes", [])}
    baselines = diff.get("baselines", {})
    for name in sorted(checks):
        c = checks[name]
        g = c.get("gates", [])
        ok = sum(1 for x in g if x.get("ok"))
        before = prev_status.get(name, c.get("status") if name in baselines else None)
        out.append(
            f'<tr><td><a href="#check-{esc(name)}">{esc(name)}</a></td><td>{pill(c.get("status"))}</td>'
            f'<td>{"required" if c.get("required", True) else "advisory"}</td><td class="num">{ok}/{len(g)}</td>'
            f'<td class="num">{fmt(c.get("wall_s"))}</td><td>{pill(before) if before else ""}</td></tr>'
        )
    out.append("</table></div>")

    out.append("<h2>Changes against earlier runs</h2>")
    if not diff.get("vs"):
        out.append(f'<p class="muted">No previous {esc(run.get("mode"))} run in the history.</p>')
    else:
        n_ch, n_st, n_noisy = (
            len(diff.get("changed", [])),
            len(diff.get("status_changes", [])),
            len(diff.get("noisy", [])),
        )
        out.append(
            f"<p>latest earlier run <code>{esc(diff['vs'])}</code>: {n_st} status change(s), {n_ch} tracked "
            f"number(s) changed, {n_noisy} timing(s) moved (not counted).</p>"
        )
        other = {n: b["metrics"] for n, b in sorted(baselines.items()) if b.get("metrics") not in (None, diff["vs"])}
        if other:
            out.append(
                '<p class="muted">compared with the last run that produced results instead: '
                + ", ".join(f"{esc(n)} <code>{esc(v)}</code>" for n, v in other.items())
                + "</p>"
            )
        if diff.get("new_checks"):
            out.append(f'<p class="muted">no earlier run of: {esc(", ".join(diff["new_checks"]))}</p>')
        if diff.get("context"):
            items = "; ".join(
                f"{esc(e['check'])} {esc(e['what'])}: {fmt(e.get('before'))} &rarr; {fmt(e.get('after'))}"
                for e in diff["context"][:20]
            )
            out.append(f'<p class="muted">context (not counted): {items}</p>')
        rows = diff.get("status_changes", []) + diff.get("changed", [])
        if rows:
            out.append('<div class="scroll"><table><tr><th>check</th><th>what</th><th>before</th><th>after</th></tr>')
            for e in diff.get("status_changes", []):
                out.append(
                    f"<tr><td>{esc(e['check'])}</td><td>status</td><td>{pill(e['before'])}</td>"
                    f"<td>{pill(e['after'])}</td></tr>"
                )
            for e in diff.get("changed", [])[:200]:
                out.append(
                    f'<tr><td>{esc(e["check"])}</td><td class="mono">{esc(e["metric"])}</td>'
                    f'<td class="num">{fmt(e.get("before"))}</td><td class="num">{fmt(e.get("after"))}</td></tr>'
                )
            out.append("</table></div>")
            if len(diff.get("changed", [])) > 200:
                out.append(f'<p class="muted">{len(diff["changed"]) - 200} more in run.json.</p>')

    out.append("<h2>Details</h2>")
    for name in sorted(checks):
        out.append(render_check(name, checks[name], diff, link_base))

    out.append(_bench_trend(history, run.get("mode"), max_history))
    out.append("<h2>History</h2>")
    names = sorted({n for r in history.get("runs", []) for n in r.get("checks", {})})
    out.append('<div class="scroll"><table><tr><th>run</th><th>mode</th><th>git</th><th>exit</th>')
    out.extend(f"<th>{esc(n)}</th>" for n in names)
    out.append("<th>changes</th></tr>")
    for r in list(reversed(history.get("runs", [])))[:max_history]:
        d = r.get("diff", {})
        rid = r.get("nightly_id")
        cell = esc(rid)
        if runs_base is not None and run_pages and rid in run_pages:
            cell = f'<a href="{esc(f"{runs_base}{rid}/index.html")}">{esc(rid)}</a>'
        out.append(
            f'<tr><td class="mono">{cell}</td><td>{esc(r.get("mode"))}</td>'
            f'<td class="mono">{esc((r.get("git", {}).get("head") or "")[:8])}</td>'
            f'<td class="num">{esc(r.get("exit_code"))}</td>'
        )
        for n in names:
            c = r.get("checks", {}).get(n)
            out.append(f"<td>{pill(c.get('status')) if c else ''}</td>")
        out.append(f'<td class="num">{len(d.get("changed", [])) + len(d.get("status_changes", []))}</td></tr>')
    out.append("</table></div>")
    out.append('<h2>Exit codes</h2><ul class="gates">')
    out.extend(f"<li><code>{k}</code> {esc(v)}</li>" for k, v in sorted(EXIT_MEANING.items()))
    out.append("</ul></main></body></html>\n")
    return "\n".join(out)
