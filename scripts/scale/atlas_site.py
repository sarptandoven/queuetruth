#!/usr/bin/env python3
"""The Fill-Realism Atlas site (ROADMAP W1): a static, self-contained site from an `atlas.py combine` directory.

    uv run --frozen --no-sync python scripts/scale/atlas_site.py --atlas ATLAS_DIR --out SITE_DIR \
        [--source nasdaq|iex] [--mode faithful] [--population standard] [--horizon 60s] [--per-symbol] \
        [--pnl-horizon 60s]

Reads `atlas_manifest.json` and `leaderboard_by_tier.parquet` (`pnl_by_tier.parquet` when the atlas has it,
`per_symbol.parquet` with `--per-symbol`) and writes:

- `index.html`: for each liquidity tier (`ALL` first), every simulator fill model's fill-rate bias against the exact
  replay at every horizon, with its 95% interval from the two-way dates x symbols bootstrap, sorted by the size of
  the bias at `--horizon` (default 60 s); in plain words first: the exact fill rate, the most optimistic and the most
  pessimistic model among those whose interval excludes 0, with the relative size, and the closest model; with
  `pnl_by_tier`, each model's P&L error (simulated minus exact P&L per order, in ticks, marked to the mid
  `--pnl-horizon` after the first fill) with its interval, and a second sentence on the largest clear one among the
  100 most traded symbols (ticks pool there; else all symbols); the flagged share of each row; how to verify every
  number. Nothing is stated as a finding unless its 95% interval excludes 0;
- `data/leaderboard.json`: the rows shown (and `pnl_rows`), for programs; `data/atlas_manifest.json`: the manifest
  as combined (day roots, atlas root, flag policy, flagged symbol-days);
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
import re
from pathlib import Path
from typing import Any

#: IEX's required attribution (IEX Historical Data Terms of Use, clause 01), as `queuetruth.demo.sources` carries it.
IEX_ATTRIBUTION = (
    "Data provided for free by IEX. By accessing or using IEX Historical Data, you agree to the IEX Historical Data "
    "Terms of Use."
)
IEX_TERMS_URL = "https://iextrading.com/iex-historical-data-terms/"
#: The guides the page links to (docs on GitHub).
DOCS = "https://github.com/sarptandoven/queuetruth/blob/main/docs"

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


def iso_date(d: str) -> str:
    """A day as YYYY-MM-DD: the pipeline names days MMDDYYYY (as the Nasdaq files do); other forms pass through."""
    d = str(d)
    return f"{d[4:]}-{d[:2]}-{d[2:4]}" if len(d) == 8 and d.isdigit() else d


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


def _rows(atlas: Path, mode: str, population: str | None) -> tuple[list[dict[str, Any]], str]:
    """The leaderboard rows of (mode, population) and the population used: `None` = `standard` when the atlas has
    it, else the first population in name order (the plan's populations, `common.SCALE_POPULATIONS`)."""
    import pyarrow.parquet as pq

    rows = pq.read_table(atlas / "leaderboard_by_tier.parquet").to_pylist()
    if population is None:
        pops = sorted({r["shadow_pop"] for r in rows if r["mode"] == mode})
        population = "standard" if "standard" in pops else (pops[0] if pops else "standard")
    keep = [r for r in rows if r["mode"] == mode and r["shadow_pop"] == population]
    if not keep:
        have = sorted({(r["mode"], r["shadow_pop"]) for r in rows})
        raise SystemExit(f"site: no leaderboard rows for mode {mode!r}, population {population!r} (have {have})")
    return keep, population


def _pnl_rows(atlas: Path, mode: str, population: str) -> list[dict[str, Any]]:
    """The `pnl_by_tier` rows of (mode, population); none when the atlas has no P&L."""
    import pyarrow.parquet as pq

    path = atlas / "pnl_by_tier.parquet"
    if not path.exists():
        return []
    rows = pq.read_table(path).to_pylist()
    return [r for r in rows if r["mode"] == mode and r["shadow_pop"] == population]


def ticks(x: float | None) -> str:
    return "n/a" if x is None or not math.isfinite(x) else f"{x:+.3f}"


#: tier names of `plan.py` in plain words
TIER_WORDS = {
    "ALL": "all symbols",
    "T1_top100": "the 100 most traded symbols",
    "T2_101_500": "the 101st to 500th most traded symbols",
    "T3_501_1500": "the 501st to 1,500th most traded symbols",
    "T4_1501_4000": "the 1,501st to 4,000th most traded symbols",
    "T5_rest": "the less traded symbols",
}


def _clear(r: dict[str, Any], metric: str) -> bool:
    """The metric's 95% interval excludes 0 (a difference the page may state as a finding)."""
    lo, hi = r.get(f"{metric}_ci_lo"), r.get(f"{metric}_ci_hi")
    return lo is not None and hi is not None and (lo > 0 or hi < 0)


def _named(model_id: str) -> str:
    lib, name = describe(model_id)
    return f"{lib}'s {name} ({model_id})"


def pnl_tier(rows: list[dict[str, Any]]) -> str | None:
    """The tier of the P&L sentence: the 100 most traded symbols when the atlas has them (a tick means about the
    same there, so ticks can be pooled), else all symbols."""
    tiers = {r["tier"] for r in rows}
    return "T1_top100" if "T1_top100" in tiers else ("ALL" if "ALL" in tiers else None)


def pnl_headline(rows: list[dict[str, Any]], horizon: int, subject: str = "a fill model") -> str | None:
    """The P&L sentence (tier `pnl_tier`): the exact P&L per order, then the non-bound model whose P&L is furthest
    from it among those whose difference is clear (95% interval excludes 0), or that none is; `subject` names the
    models of `rows` (a library page passes only its library's)."""
    tier = pnl_tier(rows)
    cands = [
        r
        for r in rows
        if r["tier"] == tier
        and r["horizon_ns"] == horizon
        and describe(r["model_id"])[0] != "bound"
        and r.get("pnl_bias_ticks_value") is not None
    ]
    if not cands:
        return None
    where = f"Among {TIER_WORDS.get(tier, 'tier ' + tier)}, marked to the mid {duration(horizon)} after each fill"
    exact = cands[0].get("exact_pnl_ticks_value")
    lead = f"{where}, the exact replay makes {ticks(exact)} ticks per order"
    clear = [r for r in cands if _clear(r, "pnl_bias_ticks")]
    if not clear:
        return f"{lead}, and no fill model's P&L differs from it beyond its 95% interval."
    r = max(clear, key=lambda r: (abs(r["pnl_bias_ticks_value"]), r["model_id"]))
    b = r["pnl_bias_ticks_value"]
    rel = f"an error of {100 * abs(b / exact):.0f}% of the exact P&L; " if exact else ""
    bps = r.get("pnl_bias_bps_value")
    return (
        f"{lead}; {_named(r['model_id'])} books {abs(b):.3f} ticks per order {'more' if b > 0 else 'less'} "
        f"({rel}95% interval {ticks(r.get('pnl_bias_ticks_ci_lo'))} to {ticks(r.get('pnl_bias_ticks_ci_hi'))}"
        + (f"; {bps:+.2f} bp" if bps is not None else "")
        + f"), the largest clear P&L error of {subject}."
    )


def _tier_order(tiers: set[str]) -> list[str]:
    return ["ALL", *sorted(t for t in tiers if t != "ALL")] if "ALL" in tiers else sorted(tiers)


#: The libraries with a page of their own: library -> (page, the question people search for, docs path of the guide
#: to calibrate it, the guide's name).
LIBRARY_PAGES = {
    "hftbacktest": (
        "hftbacktest.html",
        "How accurate is hftbacktest's queue model?",
        "docs/how-to/calibrate-hftbacktest.md",
        "calibrate hftbacktest's queue model",
    ),
    "NautilusTrader": (
        "nautilustrader.html",
        "How accurate is NautilusTrader's fill model?",
        "integrations/nautilus/README.md",
        "the NautilusTrader integration",
    ),
    "Databento": (
        "databento.html",
        "How accurate is Databento's queue position estimate?",
        "docs/how-to/databento-mbo.md",
        "score on Databento MBO data",
    ),
}
REPO = DOCS.rsplit("/docs", 1)[0]
#: Model families that are not a setting a reader of an L2 backtest can pick: hftbacktest's L3 FIFO model needs
#: order-level input, the very data the exact replay uses.
NOT_A_SETTING = ("hbt_l3_fifo",)
#: Short chart labels per family (with the setting's parameters appended).
SHORT = {
    "nt_prob_touch": "NautilusTrader touch",
    "nt_queue_l2_prob_touch": "NautilusTrader queue_position + touch",
    "nt_queue_l2": "NautilusTrader queue_position",
    "hbt_l3_fifo": "hftbacktest L3 FIFO",
    "hbt_risk_adverse": "hftbacktest RiskAdverse",
    "hbt_power3": "hftbacktest Power3",
    "hbt_power2": "hftbacktest Power2",
    "hbt_power": "hftbacktest Power",
    "hbt_log2": "hftbacktest Log2",
    "hbt_log": "hftbacktest Log",
    "databento_mc": "Databento",
}
#: Model id keys that are not a setting of the library (the zoo's mode and exchange, the Monte Carlo size).
_NOT_PARAMS = ("mode", "exch", "particles")


def parse_id(model_id: str) -> tuple[str, dict[str, str]]:
    """`hbt_power3[exch=partial;mode=faithful;n=2]` -> (`hbt_power3`, {exch: partial, mode: faithful, n: 2})."""
    fam, _, rest = model_id.partition("[")
    params: dict[str, str] = {}
    for kv in rest.rstrip("]").split(";") if rest else ():
        k, _, v = kv.partition("=")
        if k:
            params[k] = v
    return fam, params


def short_label(model_id: str) -> str:
    """`hftbacktest Power3 n=2`: the chart label of a zoo model id; unknown families keep their id."""
    fam, params = parse_id(model_id)
    if fam not in SHORT:
        return model_id
    extra = " ".join(f"{k}={v}" for k, v in sorted(params.items()) if k not in _NOT_PARAMS)
    return f"{SHORT[fam]} {extra}".strip()


def _num(text: str) -> str | None:
    """A model parameter as a Python literal (`2`, `0.3`), None when it is not a finite number."""
    try:
        x = float(text)
    except ValueError:
        return None
    return f"{x:g}" if math.isfinite(x) else None


#: zoo family -> hftbacktest `BacktestAsset` builder and whether it takes `n` (python/queuetruth/export/hftbacktest.py)
HBT_BUILDERS = {
    "hbt_power": ("power_prob_queue_model", True),
    "hbt_power2": ("power_prob_queue_model2", True),
    "hbt_power3": ("power_prob_queue_model3", True),
    "hbt_log": ("log_prob_queue_model", False),
    "hbt_log2": ("log_prob_queue_model2", False),
    "hbt_risk_adverse": ("risk_adverse_queue_model", False),
}
HBT_EXCHANGES = {"partial": ".partial_fill_exchange()", "no-partial": ".no_partial_fill_exchange()"}
#: NautilusTrader families -> `queue_position` on the venue (integrations/nautilus venue_options)
NT_QUEUE = {"nt_prob_touch": False, "nt_queue_l2_prob_touch": True, "nt_queue_l2": True}


def snippet(model_id: str) -> tuple[str, str] | None:
    """(where it goes, code) to reproduce a zoo model in its library, as `queuetruth export` and the NautilusTrader
    integration write it; None when no library setting reproduces it (corrected mode, L3 input, Databento k > 0, an
    unknown family or parameter)."""
    fam, p = parse_id(model_id)
    if p.get("mode", "faithful") != "faithful":
        return None  # corrected mode is queuetruth's fixed variant: no released library does that
    exch = HBT_EXCHANGES.get(p.get("exch", ""), "")
    if fam in HBT_BUILDERS:
        builder, takes_n = HBT_BUILDERS[fam]
        n = _num(p.get("n", "")) if takes_n else ""
        if n is None:
            return None
        return "hftbacktest BacktestAsset chain", f".{builder}({n}){exch}"
    if fam in NT_QUEUE:
        prob = _num(p.get("p", "1"))
        if prob is None:
            return None
        return (
            "NautilusTrader BacktestEngine.add_venue keywords",
            f"fill_model=FillModel(prob_fill_on_limit={prob}), book_type=BookType.L2_MBP, trade_execution=True, "
            f"queue_position={NT_QUEUE[fam]}",
        )
    if fam == "databento_mc":
        k = _num(p.get("k", ""))
        if k is None or not -1 < float(k) <= 0:
            return None
        n = f"{1 / (1 + float(k)):g}"
        return "hftbacktest approximation (expected value of the estimator)", f".power_prob_queue_model3({n})"
    return None


def _bias(r: dict[str, Any] | None) -> float | None:
    v = None if r is None else r.get("fill_rate_bias_value")
    return v if v is not None and math.isfinite(v) else None


def verdict(r: dict[str, Any]) -> str:
    """Whether a setting is still clearly off at its horizon: only a 95% interval that excludes 0 says so."""
    if _clear(r, "fill_rate_bias"):
        return (
            "still clearly off: too optimistic"
            if r["fill_rate_bias_value"] > 0
            else "still clearly off: too pessimistic"
        )
    return "no clear difference (95% interval includes 0)"


def best_settings(rows: list[dict[str, Any]], horizon: int, libraries: tuple[str, ...] | None = None) -> list[dict]:
    """For each tier (`ALL` first) and library, the setting whose fill-rate bias at `horizon` is closest to 0 (ties
    by model id), with whether its interval still excludes 0 and its snippet."""
    libs = libraries or tuple(LIBRARY_PAGES)
    out = []
    for tier in _tier_order({r["tier"] for r in rows}):
        for lib in libs:
            cands = [
                r
                for r in rows
                if r["tier"] == tier
                and r["horizon_ns"] == horizon
                and describe(r["model_id"])[0] == lib
                and parse_id(r["model_id"])[0] not in NOT_A_SETTING
                and _bias(r) is not None
            ]
            if not cands:
                continue
            r = min(cands, key=lambda r: (abs(r["fill_rate_bias_value"]), r["model_id"]))
            snip = snippet(r["model_id"])
            out.append(
                {
                    "tier": tier,
                    "library": lib,
                    "model_id": r["model_id"],
                    "horizon_ns": horizon,
                    "n_settings": len(cands),
                    "fill_rate_bias_value": r["fill_rate_bias_value"],
                    "fill_rate_bias_ci_lo": r.get("fill_rate_bias_ci_lo"),
                    "fill_rate_bias_ci_hi": r.get("fill_rate_bias_ci_hi"),
                    "clearly_off": _clear(r, "fill_rate_bias"),
                    "snippet_kind": snip[0] if snip else None,
                    "snippet": snip[1] if snip else None,
                }
            )
    return out


def _count(k: int, one: str, many: str) -> str:
    return f"{k} {one if k == 1 else many}"


def library_headline(rows: list[dict[str, Any]], lib: str, horizon: int) -> str | None:
    """A library page's fill-rate sentence (tier ALL, else the first tier): the exact fill rate, how many of the
    library's settings fill clearly more or fewer orders (95% interval excludes 0), and the closest setting with
    whether it is still clearly off."""
    tiers = _tier_order({r["tier"] for r in rows})
    best = [b for b in best_settings(rows, horizon, (lib,)) if tiers and b["tier"] == tiers[0]]
    if not best:
        return None
    b = best[0]
    tier = b["tier"]
    cands = [
        r
        for r in rows
        if r["tier"] == tier
        and r["horizon_ns"] == horizon
        and describe(r["model_id"])[0] == lib
        and parse_id(r["model_id"])[0] not in NOT_A_SETTING
        and _bias(r) is not None
    ]
    exact = cands[0].get("exact_fill_rate_value")
    where = "" if tier == "ALL" else f" among {TIER_WORDS.get(tier, 'tier ' + tier)}"
    over = sum(1 for r in cands if _clear(r, "fill_rate_bias") and r["fill_rate_bias_value"] > 0)
    under = sum(1 for r in cands if _clear(r, "fill_rate_bias") and r["fill_rate_bias_value"] < 0)
    lead = (
        f"Within {duration(horizon)}{where}, the exact replay of the real queue fills {pp(exact, False)}% of these "
        f"orders. Of {lib}'s {_count(len(cands), 'setting', 'settings')} in the atlas, "
    )
    if over or under:
        said = []
        if over:
            said.append(f"{_count(over, 'fills', 'fill')} clearly more orders")
        if under:
            said.append(f"{_count(under, 'fills', 'fill')} clearly fewer")
        lead += " and ".join(said) + " (95% interval excludes 0)"
    else:
        lead += "none fills more or fewer orders than it beyond its 95% interval"
    v = b["fill_rate_bias_value"]
    if b["clearly_off"]:
        tail = f"so even the best setting still fills clearly {'more' if v > 0 else 'fewer'} orders than the real queue"
    else:
        tail = "which the atlas cannot tell from the exact replay at this sample size"
    return (
        f"{lead}. The closest is {_named(b['model_id'])}, {pp(v)} pp (95% interval {pp(b['fill_rate_bias_ci_lo'])} "
        f"to {pp(b['fill_rate_bias_ci_hi'])} pp), {tail}."
    )


def headline(rows: list[dict[str, Any]], horizon: int) -> str | None:
    """The fill-rate sentence (tier ALL, `horizon`): the exact fill rate, the most optimistic and the most
    pessimistic non-bound models among those whose bias is clear (95% interval excludes 0), and the closest one."""
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
    exact = cands[0].get("exact_fill_rate_value")
    parts = [
        f"Within {duration(horizon)}, the exact replay of the real queue fills {pp(exact, False)}% of these orders"
    ]

    def said(r: dict[str, Any], word: str) -> str:
        b = r["fill_rate_bias_value"]
        more = "more" if b > 0 else "fewer"
        rel = f"{100 * abs(b / exact):.0f}% {more} orders; " if exact else ""
        return (
            f"the most {word} fill model, {_named(r['model_id'])}, fills {abs(100 * b):.2f} percentage points "
            f"{more} ({rel}95% interval {pp(r['fill_rate_bias_ci_lo'])} to {pp(r['fill_rate_bias_ci_hi'])} pp)"
        )

    over = [r for r in cands if _clear(r, "fill_rate_bias") and r["fill_rate_bias_value"] > 0]
    under = [r for r in cands if _clear(r, "fill_rate_bias") and r["fill_rate_bias_value"] < 0]
    if over:
        parts.append(said(max(over, key=lambda r: (r["fill_rate_bias_value"], r["model_id"])), "optimistic"))
    if under:
        parts.append(said(min(under, key=lambda r: (r["fill_rate_bias_value"], r["model_id"])), "pessimistic"))
    close = min(cands, key=lambda r: (abs(r["fill_rate_bias_value"]), r["model_id"]))
    parts.append(f"the closest is {_named(close['model_id'])}, {pp(close['fill_rate_bias_value'])} pp")
    if not over and not under:
        parts.insert(1, "no fill model differs from it beyond its 95% interval")
    return "; ".join(parts) + "."


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
a { color: var(--under); } nav.libs { margin: 8px 0; } p { overflow-wrap: anywhere; }
table.best th, table.best td { text-align: left; white-space: normal; vertical-align: top; }
table.best td.cfg { min-width: 18em; } table.best td code { overflow-wrap: anywhere; }
figure.chart { margin: 8px 0 12px; } svg.bias { width: 100%; min-width: 560px; height: auto; display: block;
  font: 12px system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
svg.bias text { fill: var(--fg); } svg.bias .tick { fill: var(--muted); }
svg.bias .grid { stroke: var(--line); stroke-width: 1; } svg.bias .zero { stroke: var(--muted); stroke-width: 1.5; }
svg.bias .ci { stroke: var(--muted); stroke-width: 2; stroke-linecap: round; }
svg.bias .dot { fill: var(--muted); stroke: var(--bg); stroke-width: 2; }
svg.bias .over .ci { stroke: var(--over); } svg.bias .over .dot { fill: var(--over); }
svg.bias .under .ci { stroke: var(--under); } svg.bias .under .dot { fill: var(--under); }
"""


SUMMARY = "How far each backtester's fill model is from the exact replay of the real order queue."


def _page(title: str, body: str, attribution: str, description: str = SUMMARY) -> str:
    """A whole page; `description` is the search and link-preview text (the headline when there is one)."""
    d = html.escape(description if len(description) <= 300 else description[:297].rstrip() + "...")
    t = html.escape(title)
    meta = (
        f'<meta name="description" content="{d}">\n<meta property="og:type" content="website">\n'
        f'<meta property="og:title" content="{t}">\n<meta property="og:description" content="{d}">\n'
        '<meta name="twitter:card" content="summary">\n'
    )
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{t}</title>\n{meta}<style>{CSS}</style>\n</head>\n<body>\n<main>\n"
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


def _tick_step(span: float) -> float:
    """A 1, 2 or 5 x 10^k step that gives about five ticks over `span`."""
    raw = span / 5
    mag = 10 ** math.floor(math.log10(raw))
    return next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)


def _f(x: float) -> str:
    """An SVG coordinate: one decimal, so the output does not depend on float printing."""
    return f"{x:.1f}"


#: chart geometry in viewBox units
CHART_W, CHART_LABEL, CHART_X0, CHART_X1, CHART_TOP, CHART_ROW = 720, 290, 300, 650, 30, 24


def chart(rows: list[dict[str, Any]], chart_id: str, title: str, horizon: int) -> str:
    """An inline SVG of the fill-rate bias of `rows` (one per model, in the order given) at `horizon`: a dot per
    model with its 95% interval as a whisker, a line at 0, coloured `--over` / `--under` only when the interval
    excludes 0. No script; every value is also in the table next to it. Empty without rows."""
    pts = []
    for r in rows:
        v = _bias(r)
        if v is None:
            continue
        lo, hi = r.get("fill_rate_bias_ci_lo"), r.get("fill_rate_bias_ci_hi")
        ok = lo is not None and hi is not None and math.isfinite(lo) and math.isfinite(hi)
        pts.append((r, 100 * v, 100 * lo if ok else None, 100 * hi if ok else None))
    if not pts:
        return ""
    vals = [x for _, v, lo, hi in pts for x in (v, lo, hi) if x is not None]
    dmin, dmax = min(0.0, *vals), max(0.0, *vals)
    if dmax - dmin < 1e-9:
        dmin, dmax = -1.0, 1.0
    step = _tick_step(dmax - dmin)
    a, b = math.floor(dmin / step - 1e-9) * step, math.ceil(dmax / step + 1e-9) * step
    ticks = [a + i * step for i in range(round((b - a) / step) + 1)]

    def x(v: float) -> float:
        return CHART_X0 + (v - a) / (b - a) * (CHART_X1 - CHART_X0)

    height = CHART_TOP + len(pts) * CHART_ROW + 30
    bottom = CHART_TOP + len(pts) * CHART_ROW
    out = [
        f'<svg class="bias" viewBox="0 0 {CHART_W} {height}" role="img" aria-labelledby="{chart_id}-t {chart_id}-d">',
        f'<title id="{chart_id}-t">{html.escape(title)}</title>',
        f'<desc id="{chart_id}-d">Fill-rate bias of {len(pts)} models at {html.escape(duration(horizon))} in '
        "percentage points, with 95% intervals; the same values are in the table below.</desc>",
    ]
    for t in ticks:
        tx = _f(x(t))
        label = "0" if abs(t) < step / 1e6 else f"{round(t, 6):+g}"
        cls = "zero" if label == "0" else "grid"
        out.append(f'<line class="{cls}" x1="{tx}" x2="{tx}" y1="{CHART_TOP - 6}" y2="{bottom}"/>')
        out.append(f'<text class="tick" x="{tx}" y="{CHART_TOP - 12}" text-anchor="middle">{label}</text>')
    for i, (r, v, lo, hi) in enumerate(pts):
        cy = CHART_TOP + i * CHART_ROW + CHART_ROW / 2
        cls = "over" if lo is not None and lo > 0 else ("under" if hi is not None and hi < 0 else "plain")
        ci = f" [{lo:+.2f}, {hi:+.2f}]" if lo is not None else ""
        label = short_label(r["model_id"])
        out.append(f'<g class="{cls}"><title>{html.escape(label)}: {v:+.2f} pp{ci}</title>')
        out.append(f'<text x="{CHART_LABEL}" y="{_f(cy + 4)}" text-anchor="end">{html.escape(label)}</text>')
        if lo is not None:
            out.append(f'<line class="ci" x1="{_f(x(lo))}" x2="{_f(x(hi))}" y1="{_f(cy)}" y2="{_f(cy)}"/>')
        out.append(f'<circle class="dot" cx="{_f(x(v))}" cy="{_f(cy)}" r="5"/>')
        out.append(f'<text x="{CHART_W - 4}" y="{_f(cy + 4)}" text-anchor="end">{v:+.2f}</text></g>')
    out.append(
        f'<text class="tick" x="{_f((CHART_X0 + CHART_X1) / 2)}" y="{height - 8}" text-anchor="middle">'
        f"fill-rate bias within {html.escape(duration(horizon))}, percentage points (model minus exact)</text>"
    )
    out.append("</svg>")
    return (
        '<figure class="chart"><div class="wrap">'
        + "".join(out)
        + "</div>"
        + (
            '<figcaption class="muted">Dot: the bias; whisker: its 95% interval. Coloured only when the interval '
            "excludes 0 (red: fills too many orders, blue: too few); grey: no clear difference. Bounds are in the "
            "table, not drawn.</figcaption></figure>"
        )
    )


def _chart_id(page_tier: str) -> str:
    return "bias-" + re.sub(r"[^A-Za-z0-9_-]", "-", page_tier)


def _cell(value: str, lo: str, hi: str, cls: str) -> str:
    return f'<td class="{cls}">{value}<br><span class="ci">[{lo}, {hi}]</span></td>'


def _cls(r: dict[str, Any], metric: str) -> str:
    lo, hi = r.get(f"{metric}_ci_lo"), r.get(f"{metric}_ci_hi")
    return "over" if lo is not None and lo > 0 else ("under" if hi is not None and hi < 0 else "")


def _tier_section(
    tier: str,
    tr: list[dict[str, Any]],
    horizons: list[int],
    h: int,
    pnl_at: dict[tuple[str, str], dict[str, Any]] | None,
    ph: int,
    chart_title: str,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """A tier's heading, chart and table (models sorted by the size of the bias at `h`); with the rows shown."""
    models = sorted({r["model_id"] for r in tr})
    at = {(r["model_id"], r["horizon_ns"]): r for r in tr}

    def key(m: str) -> tuple:
        v = _bias(at.get((m, h)))
        return (-(abs(v) if v is not None else -1.0), m)

    order = sorted(models, key=key)
    head = "".join(f"<th>{html.escape(duration(x))}</th>" for x in horizons)
    shown, shown_pnl, body_rows = [], [], []
    for m in order:
        lib, name = describe(m)
        cells = []
        for x in horizons:
            r = at.get((m, x))
            if _bias(r) is None:
                cells.append("<td>n/a</td>")
                continue
            assert r is not None
            cells.append(
                _cell(pp(r["fill_rate_bias_value"]), pp(r.get("fill_rate_bias_ci_lo")),
                      pp(r.get("fill_rate_bias_ci_hi")), _cls(r, "fill_rate_bias"))
            )  # fmt: skip
            shown.append({k: r.get(k) for k in sorted(r)})
        if pnl_at is not None:
            pr = pnl_at.get((tier, m))
            if pr is None or pr.get("pnl_bias_ticks_value") is None:
                cells.append("<td>n/a</td>")
            else:
                cells.append(
                    _cell(ticks(pr["pnl_bias_ticks_value"]), ticks(pr.get("pnl_bias_ticks_ci_lo")),
                          ticks(pr.get("pnl_bias_ticks_ci_hi")), _cls(pr, "pnl_bias_ticks"))
                )  # fmt: skip
                shown_pnl.append({k: pr.get(k) for k in sorted(pr)})
        share = (at.get((m, h)) or {}).get("flagged_share")
        body_rows.append(
            f"<tr><td>{html.escape(lib)}</td><td>{html.escape(name)}<br><code>{html.escape(m)}</code></td>"
            + "".join(cells)
            + f"<td>{'' if share is None else f'{100 * share:.1f}%'}</td></tr>"
        )
    n = max((r["n"] for r in tr), default=0)
    pnl_head = f"<th>P&amp;L, ticks ({html.escape(duration(ph))})</th>" if pnl_at is not None else ""
    drawn = [at[(m, h)] for m in order if (m, h) in at and describe(m)[0] != "bound"]
    page = (
        f'<h2>Tier {html.escape(tier)}</h2><p class="muted">{n:,} orders.</p>'
        + chart(drawn, _chart_id(tier), chart_title, h)
        + f'<div class="wrap"><table><thead><tr><th>library</th><th>model</th>{head}{pnl_head}<th>flagged</th>'
        f"</tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>"
    )
    return page, shown, shown_pnl


def _best_section(
    best: list[dict[str, Any]], h: int, pnl_at: dict[tuple[str, str], dict[str, Any]] | None, ph: int, lib: str | None
) -> str:
    """The "best setting by tier" table: per tier (and library on the index), the setting closest to the exact
    fill rate at `h`, whether it is still clearly off, its P&L error and the config to paste."""
    if not best:
        return ""
    rows = []
    for b in best:
        cfg = (
            f'<span class="muted">{html.escape(b["snippet_kind"])}</span><br><code>{html.escape(b["snippet"])}</code>'
            if b["snippet"]
            else '<span class="muted">no library setting reproduces it: use the model id (see the guide)</span>'
        )
        pcell = ""
        if pnl_at is not None:
            pr = pnl_at.get((b["tier"], b["model_id"]))
            pcell = (
                "<td>n/a</td>"
                if pr is None or pr.get("pnl_bias_ticks_value") is None
                else _cell(ticks(pr["pnl_bias_ticks_value"]), ticks(pr.get("pnl_bias_ticks_ci_lo")),
                           ticks(pr.get("pnl_bias_ticks_ci_hi")), _cls(pr, "pnl_bias_ticks"))
            )  # fmt: skip
        guide = LIBRARY_PAGES[b["library"]][0]
        rows.append(
            f"<tr><td>{html.escape(b['tier'])}</td>"
            + ("" if lib else f'<td><a href="{guide}">{html.escape(b["library"])}</a></td>')
            + f"<td>{html.escape(short_label(b['model_id']))}<br><code>{html.escape(b['model_id'])}</code></td>"
            + _cell(pp(b["fill_rate_bias_value"]), pp(b["fill_rate_bias_ci_lo"]), pp(b["fill_rate_bias_ci_hi"]),
                    _cls(b, "fill_rate_bias"))
            + f"<td>{html.escape(verdict(b))}</td>{pcell}"
            + f'<td class="cfg">{cfg}</td></tr>'
        )  # fmt: skip
    pnl_head = f"<th>P&amp;L, ticks ({html.escape(duration(ph))})</th>" if pnl_at is not None else ""
    libs = sorted({b["library"] for b in best})
    guides = ", ".join(f'<a href="{REPO}/{LIBRARY_PAGES[x][2]}">{html.escape(LIBRARY_PAGES[x][3])}</a>' for x in libs)
    nt = (
        " <code>FillModel</code> is <code>nautilus_trader.backtest.models.FillModel</code> in 1.x and "
        "<code>nautilus_trader.execution.DefaultFillModel</code> in 2.x; the keywords go to "
        "<code>BacktestEngine.add_venue</code> (2.x also needs a <code>fee_model</code>). The faithful models follow "
        "nautilus_trader 1.231.0 on a delta-fed L2 book."
        if "NautilusTrader" in libs
        else ""
    )
    hbt = (
        " The hftbacktest line goes on your <code>BacktestAsset()</code> chain in place of its queue model and "
        "exchange (hftbacktest 2.4.4, which faithful mode reproduces)."
        if "hftbacktest" in libs or "Databento" in libs
        else ""
    )
    return (
        f"<h2>{'Best setting by tier' if lib is None else f'Best {html.escape(lib)} setting by tier'}</h2>"
        f"<p>Which setting fixes it: for each tier{'' if lib else ' and library'}, the setting whose fill-rate bias "
        f"within {html.escape(duration(h))} is closest to 0. It is <em>still clearly off</em> when its 95% interval "
        "excludes 0; otherwise the atlas cannot tell it from the exact replay at this sample size, which is not proof "
        "that it is exact. hftbacktest's L3 FIFO model is left out: it needs order-level data.</p>"
        '<div class="wrap"><table class="best"><thead><tr><th>tier</th>'
        f"{'' if lib else '<th>library</th>'}<th>closest setting</th>"
        f"<th>bias ({html.escape(duration(h))})</th><th>verdict</th>{pnl_head}<th>config to paste</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
        f'<p class="muted">The setting is the one closest at this venue, horizon and population; refit on your own '
        f"data before relying on it ({guides}).{hbt}{nt}</p>"
    )


def _library_nav(present: list[str], here: str | None) -> str:
    links = [
        html.escape(lib) if lib == here else f'<a href="{LIBRARY_PAGES[lib][0]}">{html.escape(lib)}</a>'
        for lib in present
    ]
    back = "" if here is None else '<a href="index.html">Fill-Realism Atlas</a> | '
    return f'<nav class="libs">{back}By library: {", ".join(links)}</nav>'


def build(
    atlas: Path,
    out: Path,
    *,
    source: str = "nasdaq",
    mode: str = "faithful",
    population: str | None = None,
    horizon: str | None = None,
    per_symbol: bool = False,
    pnl_horizon: str = "60s",
) -> dict[str, Any]:
    """Writes the site; returns {pages, rows, pnl_rows, atlas_root}."""
    if source not in ("nasdaq", "iex"):
        raise SystemExit(f"site: source must be nasdaq or iex, got {source!r}")
    manifest = json.loads((atlas / "atlas_manifest.json").read_text(encoding="utf-8"))
    rows, population = _rows(atlas, mode, population)
    horizons = sorted({r["horizon_ns"] for r in rows})
    if horizon is None:  # 60 s when the atlas has it: at 1 s too few orders fill for the differences to read
        h = 60_000_000_000 if 60_000_000_000 in horizons else horizons[-1]
    else:
        h = parse_duration(horizon)
    if h not in horizons:
        raise SystemExit(f"site: horizon {horizon} not in the atlas ({', '.join(duration(x) for x in horizons)})")
    tiers = _tier_order({r["tier"] for r in rows})
    pnl = _pnl_rows(atlas, mode, population)
    ph = parse_duration(pnl_horizon)
    if pnl and ph not in {r["horizon_ns"] for r in pnl}:
        have = ", ".join(duration(x) for x in sorted({r["horizon_ns"] for r in pnl}))
        raise SystemExit(f"site: P&L horizon {pnl_horizon} not in the atlas ({have})")
    pnl_at = {(r["tier"], r["model_id"]): r for r in pnl if r["horizon_ns"] == ph} if pnl else None
    days = manifest.get("days") or []
    first, last = (iso_date(days[0]["date"]), iso_date(days[-1]["date"])) if days else ("?", "?")
    span = first if len(days) == 1 else f"{len(days)} days from {first} to {last}"
    attribution = _attribution(source)
    venue = "IEX DEEP+" if source == "iex" else "Nasdaq TotalView-ITCH"
    present = [lib for lib in LIBRARY_PAGES if any(describe(r["model_id"])[0] == lib for r in rows)]
    best = best_settings(rows, h)

    about = (
        f"{html.escape(venue)}, {html.escape(span)}, "
        f"{manifest.get('symbol_days', 0):,} symbol-days, {manifest.get('distinct_symbols', 0):,} symbols; "
        f"{html.escape(mode)} mode, shadow population {html.escape(population)} "
        f'(<a href="{DOCS}/concepts/fill-realism-atlas.md">how to read it, how it is made, its limits</a>).'
    )
    pnl_days = manifest.get("pnl_days") or []
    some = ""
    if len(pnl_days) < len(days):
        some = f" (over the {len(pnl_days)} of {len(days)} days whose units computed it)"
    pnl_note = (
        f" <em>P&amp;L</em>: the backtest's P&amp;L minus the exact replay's, per order placed, in ticks, each fill "
        f"marked to the mid {html.escape(duration(ph))} later; positive: the backtest's P&amp;L is too high{some}."
        if pnl
        else ""
    )
    explain = (
        "<p>Positive bias: the model fills orders the real queue would not have filled (an optimistic backtest); "
        "negative: it misses fills. Intervals are 95%, from a bootstrap over dates and symbols. Rows are sorted by "
        f"the size of the bias at {html.escape(duration(h))}.{pnl_note} <em>flagged</em> is the share of a row's "
        "orders on symbol-days whose replay checks raised a flag or that were not in continuous trading.</p>"
    )
    parts = [
        "<h1>Fill-Realism Atlas</h1>",
        '<p class="muted">How far each backtester\'s fill model is from the exact replay of the real queue: '
        + about
        + "</p>",
    ]
    if present:
        parts.append(_library_nav(present, None))
    leads = [x for x in (headline(rows, h), pnl_headline(pnl, ph)) if x]
    for line in leads:
        parts.append(f'<p class="lead">{html.escape(line, quote=False)}</p>')
    parts.append(explain)
    parts.append(_best_section(best, h, pnl_at, ph, None))
    shown_pnl: list[dict[str, Any]] = []
    shown: list[dict[str, Any]] = []
    for tier in tiers:
        page, s, sp = _tier_section(
            tier,
            [r for r in rows if r["tier"] == tier],
            horizons,
            h,
            pnl_at,
            ph,
            f"Fill-rate bias by model within {duration(h)}, tier {tier}",
        )
        parts.append(page)
        shown += s
        shown_pnl += sp
    parts.append(
        "<h2>Check your own backtester</h2><ul>"
        f"<li>See it in a minute: <code>pipx run queuetruth demo --yes</code> places small orders in the real queue "
        f'of a public day and prints the same comparison (<a href="{DOCS}/getting-started.md">getting started</a>).'
        "</li>"
        "<li>Your simulator against your live fills, order by order: "
        f'<a href="{DOCS}/how-to/import-live-logs.md">import the live log</a>, then <code>queuetruth score</code> '
        f'(<a href="{DOCS}/how-to/decompose-the-sim-live-gap.md">where the gap comes from</a>).</li>'
        "<li>Your simulator in CI: the <code>referee</code> GitHub Action runs it on 16 known fill-bug fixtures "
        f'(<a href="{DOCS}/how-to/referee-your-simulator.md">guide</a>).</li>'
        "<li>A fitted fill model to paste back into hftbacktest or NautilusTrader: "
        f'<a href="{DOCS}/how-to/calibrate-hftbacktest.md">calibrate</a>.</li></ul>'
    )
    root = manifest.get("atlas_root", "")
    day_rows = "".join(
        f"<tr><td>{html.escape(iso_date(d['date']))}</td><td><code>{html.escape(str(d['merkle_root']))}</code></td>"
        f"<td>{d.get('units_valid', '')} / {d.get('units_planned', '')}</td></tr>"
        for d in days
    )
    built = (
        '<p class="muted">Built by <a href="https://github.com/sarptandoven/queuetruth">queuetruth</a> '
        f"{html.escape(str((manifest.get('build') or {}).get('queuetruth', '')))} from "
        '<code>scripts/scale/atlas.py</code> outputs. Data: <a href="data/leaderboard.json">leaderboard.json</a>, '
        '<a href="data/atlas_manifest.json">atlas_manifest.json</a>.'
    )
    parts.append(
        "<h2>Verify any number</h2>"
        f"<p>Atlas root <code>{html.escape(root)}</code> (Merkle root over the day roots in date order; flag policy "
        f"<code>{html.escape(str(manifest.get('flag_policy')))}</code>). Each day root covers that day's shard leaves; "
        "re-run any unit from the public day file and check its leaf against the published root:</p>"
        "<pre>uv run --frozen --no-sync python scripts/scale/verify_unit.py --help</pre>"
        f'<div class="wrap"><table><thead><tr><th>date</th><th>day root</th><th>units</th></tr></thead>'
        f"<tbody>{day_rows}</tbody></table></div>"
        + built
        + (' Per symbol: <a href="symbols.html">symbols</a>.' if per_symbol else "")
        + "</p>"
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "data").mkdir(exist_ok=True)
    page = _page("Fill-Realism Atlas", "\n".join(parts), attribution, leads[0] if leads else SUMMARY)
    (out / "index.html").write_text(page, encoding="utf-8")
    data = {
        "schema": "qt.atlas_site_leaderboard",
        "schema_version": 1,
        "atlas_root": root,
        "source": source,
        "mode": mode,
        "population": population,
        "rows": sorted(shown, key=lambda r: (r["tier"], r["model_id"], r["horizon_ns"])),
        "best_settings": best,
    }
    if pnl:
        data["pnl_horizon_ns"] = ph
        data["pnl_rows"] = sorted(shown_pnl, key=lambda r: (r["tier"], r["model_id"]))
    if source == "iex":
        data["attribution"] = IEX_ATTRIBUTION
    (out / "data" / "leaderboard.json").write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    (out / "data" / "atlas_manifest.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    pages = ["index.html"]
    for lib in present:
        pages.append(
            _library_page(
                out, lib, rows, pnl, pnl_at, horizons, tiers, h, ph, best, present, about, explain, built, attribution
            )
        )
    if per_symbol:
        pages.append(_symbols(atlas, out, mode, population, h, attribution))
    return {"pages": pages, "rows": len(shown), "pnl_rows": len(shown_pnl), "atlas_root": root}


def _library_page(
    out: Path,
    lib: str,
    rows: list[dict[str, Any]],
    pnl: list[dict[str, Any]],
    pnl_at: dict[tuple[str, str], dict[str, Any]] | None,
    horizons: list[int],
    tiers: list[str],
    h: int,
    ph: int,
    best: list[dict[str, Any]],
    present: list[str],
    about: str,
    explain: str,
    built: str,
    attribution: str,
) -> str:
    """`hftbacktest.html` and the like: one library's models across tiers and horizons (fill rate and P&L), its
    best setting by tier and a plain-words summary; titled with the question people search for."""
    name, question, guide, guide_name = LIBRARY_PAGES[lib]
    mine = [r for r in rows if describe(r["model_id"])[0] == lib]
    my_pnl = [r for r in pnl if describe(r["model_id"])[0] == lib]
    leads = [x for x in (library_headline(mine, lib, h), pnl_headline(my_pnl, ph, f"{lib}'s settings")) if x]
    parts = [
        _library_nav(present, lib),
        f"<h1>{html.escape(question)}</h1>",
        f'<p class="muted">{html.escape(lib)}\'s fill models against the exact replay of the real queue: {about}</p>',
        *(f'<p class="lead">{html.escape(line, quote=False)}</p>' for line in leads),
        explain,
        _best_section([b for b in best if b["library"] == lib], h, pnl_at, ph, lib),
    ]
    for tier in tiers:
        tr = [r for r in mine if r["tier"] == tier]
        if tr:
            parts.append(
                _tier_section(tier, tr, horizons, h, pnl_at, ph, f"{lib}: fill-rate bias within {duration(h)}, "
                              f"tier {tier}")[0]
            )  # fmt: skip
    parts.append(
        f"<h2>Use it in {html.escape(lib)}</h2><ul>"
        f'<li>Fit the setting on your own venue and data: <a href="{REPO}/{guide}">{html.escape(guide_name)}</a>.</li>'
        f'<li>What each model does: <a href="{DOCS}/concepts/l2-model-zoo.md">the L2 model zoo</a>.</li>'
        f'<li>Every library side by side: <a href="index.html">the Fill-Realism Atlas</a>.</li></ul>' + built + "</p>"
    )
    title = f"{question} Fill-Realism Atlas"
    page = _page(title, "\n".join(parts), attribution, leads[0] if leads else SUMMARY)
    (out / name).write_text(page, encoding="utf-8")
    return name


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
    ap.add_argument("--population", help="shadow population of the plan (default standard, else the first)")
    ap.add_argument(
        "--horizon", help="sort and headline horizon, e.g. 1s, 100ms, 1min (default 60 s, else the longest)"
    )
    ap.add_argument("--per-symbol", action="store_true", help="also write symbols.html from per_symbol.parquet")
    ap.add_argument("--pnl-horizon", default="60s", help="markout horizon of the P&L column (atlases with P&L)")
    a = ap.parse_args(argv)
    r = build(
        a.atlas,
        a.out,
        source=a.source,
        mode=a.mode,
        population=a.population,
        horizon=a.horizon,
        per_symbol=a.per_symbol,
        pnl_horizon=a.pnl_horizon,
    )
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
