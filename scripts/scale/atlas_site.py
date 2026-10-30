#!/usr/bin/env python3
"""The Fill-Realism Atlas site (ROADMAP W1): a static, self-contained site from an `atlas.py combine` directory.

    uv run --frozen --no-sync python scripts/scale/atlas_site.py --atlas ATLAS_DIR --out SITE_DIR \
        [--source nasdaq|iex] [--mode faithful] [--population never_cancel] [--horizon 1s] [--per-symbol]

Reads `atlas_manifest.json` and `leaderboard_by_tier.parquet` (and `per_symbol.parquet` with `--per-symbol`) and
writes:

- `index.html`: for each liquidity tier (`ALL` first), every simulator fill model's fill-rate bias against the exact
  replay at every horizon, with its 95% interval from the two-way dates x symbols bootstrap, sorted by the size of
  the bias at `--horizon`; in plain words first ("NautilusTrader's default touch model fills 3.1 pp more orders
  within 1 s than the real queue"); the flagged share of each row; how to verify every number;
- `data/leaderboard.json`: the rows shown, for programs; `data/atlas_manifest.json`: the manifest as combined (day
  roots, atlas root, flag policy, flagged symbol-days);
- `symbols.html` with `--per-symbol`: per-symbol cross-day bias with empirical-Bayes shrinkage.

The page loads nothing from the network and runs no script. Output is byte-identical for the same inputs (no
wall-clock values; the build information comes from the manifest). With `--source iex` every page carries IEX's
required attribution line and link (docs/data-and-licensing.md); publish Nasdaq-derived pages only as aggregates and
after checking the terms that apply to you.
"""

from __future__ import annotations

import argparse
import html
import json
import math
from pathlib import Path
from typing import Any

#: IEX's required attribution (IEX Historical Data Terms of Use, clause 01), as `queuetruth.demo.sources` carries it.
IEX_ATTRIBUTION = (
    "Data provided for free by IEX. By accessing or using IEX Historical Data, you agree to the IEX Historical Data "
    "Terms of Use."
)
IEX_TERMS_URL = "https://iextrading.com/iex-historical-data-terms/"

#: Model id prefix -> (library, plain-words name).
LIBRARIES = (
    ("nt_prob_touch", "NautilusTrader", "touch fill model (prob_fill_on_limit)"),
    ("nt_queue_l2_prob_touch", "NautilusTrader", "queue position + touch probability"),
    ("nt_queue_l2", "NautilusTrader", "queue_position model"),
    ("hbt_l3_fifo", "hftbacktest", "L3 FIFO queue model (order-level input)"),
    ("hbt_risk_adverse", "hftbacktest", "RiskAdverse queue model"),
    ("hbt_power3", "hftbacktest", "ProbQueue Power3"),
    ("hbt_power2", "hftbacktest", "ProbQueue Power2"),
    ("hbt_power", "hftbacktest", "ProbQueue Power"),
    ("hbt_log2", "hftbacktest", "ProbQueue Log2"),
    ("hbt_log", "hftbacktest", "ProbQueue Log"),
    ("databento_mc", "Databento", "queue-position estimator (tutorial)"),
    ("front_of_queue", "bound", "front of the queue (upper bound)"),
    ("trade_through_only", "bound", "trade-through only (lower bound)"),
)

UNITS = (("ms", 1_000_000), ("s", 1_000_000_000), ("min", 60_000_000_000))


def describe(model_id: str) -> tuple[str, str]:
    """(library, plain-words name) of a zoo model id; unknown families keep their id."""
    for prefix, lib, name in LIBRARIES:
        if model_id == prefix or model_id.startswith(prefix + "["):
            return lib, name
    return "other", model_id


def duration(ns: int) -> str:
    """`100 ms`, `1 s`, `5 min`."""
    if ns >= 60_000_000_000 and ns % 60_000_000_000 == 0:
        return f"{ns // 60_000_000_000} min"
    if ns >= 1_000_000_000 and ns % 1_000_000_000 == 0:
        return f"{ns // 1_000_000_000} s"
    return f"{ns / 1_000_000:g} ms"


def parse_duration(text: str) -> int:
    t = text.strip().replace(" ", "")
    for unit, scale in sorted(UNITS, key=lambda u: -len(u[0])):
        if t.endswith(unit):
            return int(float(t[: -len(unit)]) * scale)
    return int(t)


def pp(x: float | None, signed: bool = True) -> str:
    if x is None or not math.isfinite(x):
        return "n/a"
    return f"{100 * x:+.2f}" if signed else f"{100 * x:.2f}"


def _rows(atlas: Path, mode: str, population: str) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    rows = pq.read_table(atlas / "leaderboard_by_tier.parquet").to_pylist()
    keep = [r for r in rows if r["mode"] == mode and r["shadow_pop"] == population]
    if not keep:
        have = sorted({(r["mode"], r["shadow_pop"]) for r in rows})
        raise SystemExit(f"site: no leaderboard rows for mode {mode!r}, population {population!r} (have {have})")
    return keep


def _tier_order(tiers: set[str]) -> list[str]:
    return ["ALL", *sorted(t for t in tiers if t != "ALL")] if "ALL" in tiers else sorted(tiers)


def headline(rows: list[dict[str, Any]], horizon: int) -> str | None:
    """One sentence on the most biased non-bound model in tier ALL at `horizon`."""
    cands = [
        r
        for r in rows
        if r["tier"] == "ALL"
        and r["horizon_ns"] == horizon
        and describe(r["model_id"])[0] != "bound"
        and r.get("fill_rate_bias_value") is not None
    ]
    if not cands:
        return None
    r = max(cands, key=lambda r: (abs(r["fill_rate_bias_value"]), r["model_id"]))
    lib, name = describe(r["model_id"])
    b = r["fill_rate_bias_value"]
    more = "more" if b > 0 else "fewer"
    return (
        f"{lib}'s {name} ({r['model_id']}) fills {abs(100 * b):.2f} percentage points {more} orders within "
        f"{duration(horizon)} than the exact replay of the real queue (95% interval {pp(r['fill_rate_bias_ci_lo'])} "
        f"to {pp(r['fill_rate_bias_ci_hi'])} pp; exact fill rate {pp(r.get('exact_fill_rate_value'), False)}%)."
    )


CSS = """
:root { --fg: #1b1f24; --muted: #57606a; --bg: #ffffff; --line: #d0d7de; --over: #b42318; --under: #175cd3;
        --chip: #f6f8fa; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e6edf3; --muted: #9da7b3; --bg: #0d1117; --line: #30363d; --over: #ff7b72; --under: #79c0ff;
          --chip: #161b22; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
       font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 1.6rem; margin: 0 0 4px; } h2 { font-size: 1.2rem; margin: 32px 0 8px; }
p.lead { font-size: 1.1rem; } .muted { color: var(--muted); }
.wrap { overflow-x: auto; } table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { padding: 6px 8px; border-bottom: 1px solid var(--line); text-align: right; white-space: nowrap; }
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; }
td.over { color: var(--over); } td.under { color: var(--under); } .ci { color: var(--muted); font-size: .85em; }
code { background: var(--chip); padding: 1px 4px; border-radius: 4px; }
pre { background: var(--chip); padding: 12px; border-radius: 6px; overflow-x: auto; }
.attr { border-left: 3px solid var(--line); padding-left: 12px; }
"""


def _page(title: str, body: str, attribution: str) -> str:
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n<main>\n"
        f"{body}\n{attribution}\n</main>\n</body>\n</html>\n"
    )


def _attribution(source: str) -> str:
    if source != "iex":
        return (
            '<p class="muted attr">Aggregate statistics computed from Nasdaq TotalView-ITCH 5.0 sample files. '
            "No market data is published here.</p>"
        )
    return (
        f'<p class="attr">{html.escape(IEX_ATTRIBUTION)} '
        f'<a href="{IEX_TERMS_URL}">IEX Historical Data Terms of Use</a></p>'
    )


def build(
    atlas: Path,
    out: Path,
    *,
    source: str = "nasdaq",
    mode: str = "faithful",
    population: str = "never_cancel",
    horizon: str = "1s",
    per_symbol: bool = False,
) -> dict[str, Any]:
    """Writes the site; returns {pages, rows, atlas_root}."""
    if source not in ("nasdaq", "iex"):
        raise SystemExit(f"site: source must be nasdaq or iex, got {source!r}")
    manifest = json.loads((atlas / "atlas_manifest.json").read_text(encoding="utf-8"))
    rows = _rows(atlas, mode, population)
    horizons = sorted({r["horizon_ns"] for r in rows})
    h = parse_duration(horizon)
    if h not in horizons:
        raise SystemExit(f"site: horizon {horizon} not in the atlas ({', '.join(duration(x) for x in horizons)})")
    tiers = _tier_order({r["tier"] for r in rows})
    days = manifest.get("days") or []
    first, last = (days[0]["date"], days[-1]["date"]) if days else ("?", "?")
    attribution = _attribution(source)
    venue = "IEX DEEP+" if source == "iex" else "Nasdaq TotalView-ITCH"

    parts = [
        "<h1>Fill-Realism Atlas</h1>",
        f'<p class="muted">How far each backtester\'s fill model is from the exact replay of the real queue: '
        f"{html.escape(venue)}, {len(days)} day(s) from {html.escape(first)} to {html.escape(last)}, "
        f"{manifest.get('symbol_days', 0):,} symbol-days, {manifest.get('distinct_symbols', 0):,} symbols; "
        f"{html.escape(mode)} mode, {html.escape(population.replace('_', '-'))} orders.</p>",
    ]
    line = headline(rows, h)
    if line:
        parts.append(f'<p class="lead">{html.escape(line, quote=False)}</p>')
    parts.append(
        "<p>Positive bias: the model fills orders the real queue would not have filled (an optimistic backtest); "
        "negative: it misses fills. Intervals are 95%, from a bootstrap over dates and symbols. Rows are sorted by "
        f"the size of the bias at {html.escape(duration(h))}. <em>flagged</em> is the share of a row's orders on "
        "symbol-days whose replay checks raised a flag or that were not in continuous trading.</p>"
    )
    shown = []
    for tier in tiers:
        tr = [r for r in rows if r["tier"] == tier]
        models = sorted({r["model_id"] for r in tr})
        at = {(r["model_id"], r["horizon_ns"]): r for r in tr}

        def key(m: str, at: dict = at) -> tuple:
            v = (at.get((m, h)) or {}).get("fill_rate_bias_value")
            return (-(abs(v) if v is not None else -1.0), m)

        head = "".join(f"<th>{html.escape(duration(x))}</th>" for x in horizons)
        body_rows = []
        for m in sorted(models, key=key):
            lib, name = describe(m)
            cells = []
            for x in horizons:
                r = at.get((m, x))
                if r is None or r.get("fill_rate_bias_value") is None:
                    cells.append("<td>n/a</td>")
                    continue
                v = r["fill_rate_bias_value"]
                cls = (
                    "over"
                    if r.get("fill_rate_bias_ci_lo", 0) > 0
                    else ("under" if r.get("fill_rate_bias_ci_hi", 0) < 0 else "")
                )
                cells.append(
                    f'<td class="{cls}">{pp(v)}<br><span class="ci">[{pp(r.get("fill_rate_bias_ci_lo"))}, '
                    f"{pp(r.get('fill_rate_bias_ci_hi'))}]</span></td>"
                )
                shown.append({k: r.get(k) for k in sorted(r)})
            ref = at.get((m, h)) or {}
            share = ref.get("flagged_share")
            body_rows.append(
                f"<tr><td>{html.escape(lib)}</td><td>{html.escape(name)}<br><code>{html.escape(m)}</code></td>"
                + "".join(cells)
                + f"<td>{'' if share is None else f'{100 * share:.1f}%'}</td></tr>"
            )
        n = max((r["n"] for r in tr), default=0)
        parts.append(
            f'<h2>Tier {html.escape(tier)}</h2><p class="muted">{n:,} orders.</p>'
            f'<div class="wrap"><table><thead><tr><th>library</th><th>model</th>{head}<th>flagged</th></tr></thead>'
            f"<tbody>{''.join(body_rows)}</tbody></table></div>"
        )
    root = manifest.get("atlas_root", "")
    day_rows = "".join(
        f"<tr><td>{html.escape(str(d['date']))}</td><td><code>{html.escape(str(d['merkle_root']))}</code></td>"
        f"<td>{d.get('units_valid', '')} / {d.get('units_planned', '')}</td></tr>"
        for d in days
    )
    parts.append(
        "<h2>Verify any number</h2>"
        f"<p>Atlas root <code>{html.escape(root)}</code> (Merkle root over the day roots in date order; flag policy "
        f"<code>{html.escape(str(manifest.get('flag_policy')))}</code>). Each day root covers that day's shard leaves; "
        "re-run any unit from the public day file and check its leaf against the published root:</p>"
        "<pre>uv run --frozen --no-sync python scripts/scale/verify_unit.py --help</pre>"
        f'<div class="wrap"><table><thead><tr><th>date</th><th>day root</th><th>units</th></tr></thead>'
        f"<tbody>{day_rows}</tbody></table></div>"
        '<p class="muted">Built by <a href="https://github.com/sarptandoven/queuetruth">queuetruth</a> '
        f"{html.escape(str((manifest.get('build') or {}).get('queuetruth', '')))} from "
        '<code>scripts/scale/atlas.py</code> outputs. Data: <a href="data/leaderboard.json">leaderboard.json</a>, '
        '<a href="data/atlas_manifest.json">atlas_manifest.json</a>.'
        + (' Per symbol: <a href="symbols.html">symbols</a>.' if per_symbol else "")
        + "</p>"
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "data").mkdir(exist_ok=True)
    (out / "index.html").write_text(_page("Fill-Realism Atlas", "\n".join(parts), attribution), encoding="utf-8")
    data = {
        "schema": "qt.atlas_site_leaderboard",
        "schema_version": 1,
        "atlas_root": root,
        "source": source,
        "mode": mode,
        "population": population,
        "rows": sorted(shown, key=lambda r: (r["tier"], r["model_id"], r["horizon_ns"])),
    }
    if source == "iex":
        data["attribution"] = IEX_ATTRIBUTION
    (out / "data" / "leaderboard.json").write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    (out / "data" / "atlas_manifest.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    pages = ["index.html"]
    if per_symbol:
        pages.append(_symbols(atlas, out, mode, population, h, attribution))
    return {"pages": pages, "rows": len(shown), "atlas_root": root}


def _symbols(atlas: Path, out: Path, mode: str, population: str, h: int, attribution: str) -> str:
    import pyarrow.parquet as pq

    per = [
        r
        for r in pq.read_table(atlas / "per_symbol.parquet").to_pylist()
        if r.get("mode") == mode and r.get("shadow_pop") == population and r.get("horizon_ns") == h
    ]
    per.sort(key=lambda r: (r["symbol"], r["model_id"]))
    keys = [k for k in ("bias", "ci_lo", "ci_hi", "bias_shrunk") if per and k in per[0]]
    head = "".join(f"<th>{html.escape(k)}</th>" for k in keys)
    body = "".join(
        f"<tr><td>{html.escape(str(r['symbol']))}</td><td><code>{html.escape(r['model_id'])}</code></td>"
        + "".join(f"<td>{pp(r.get(k))}</td>" for k in keys)
        + "</tr>"
        for r in per
    )
    page = (
        f'<h1>Per symbol at {html.escape(duration(h))}</h1><p><a href="index.html">Back to the atlas</a></p>'
        f'<div class="wrap"><table><thead><tr><th>symbol</th><th>model</th>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )
    (out / "symbols.html").write_text(_page("Fill-Realism Atlas: per symbol", page, attribution), encoding="utf-8")
    return "symbols.html"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--atlas", type=Path, required=True, help="an `atlas.py combine` output directory")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--source", choices=("nasdaq", "iex"), default="nasdaq")
    ap.add_argument("--mode", default="faithful")
    ap.add_argument("--population", default="never_cancel")
    ap.add_argument("--horizon", default="1s", help="sort and headline horizon, e.g. 1s, 100ms, 1min")
    ap.add_argument("--per-symbol", action="store_true", help="also write symbols.html from per_symbol.parquet")
    a = ap.parse_args(argv)
    r = build(
        a.atlas,
        a.out,
        source=a.source,
        mode=a.mode,
        population=a.population,
        horizon=a.horizon,
        per_symbol=a.per_symbol,
    )
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
