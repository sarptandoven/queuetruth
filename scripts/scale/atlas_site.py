#!/usr/bin/env python3
"""The Fill-Realism Atlas site (ROADMAP W1): a static, self-contained site from an `atlas.py combine` directory.

    uv run --frozen --no-sync python scripts/scale/atlas_site.py --atlas ATLAS_DIR --out SITE_DIR \
        [--source nasdaq|iex] [--mode faithful] [--population standard] [--horizon 60s] [--per-symbol] \
        [--pnl-horizon 60s] [--built-at DATE]

Reads `atlas_manifest.json`, `leaderboard_by_tier.parquet`, `pnl_by_tier.parquet` (when the atlas has it) and
`per_symbol.parquet` (when it has it), and writes:

- `index.html`, the answer first: the exact fill rate and one plain sentence per library (human labels such as
  "NautilusTrader default (touch fill)"; model ids only in tooltips and details), one chart of every library's
  settings at `--horizon` (default 60 s) with 95% intervals, a "what to do" box per library (closest setting,
  verdict, config to paste), the symbol lookup, the venue's scope, and the last-built date with the atlas root and
  how to verify it. Nothing is stated as a finding unless its 95% interval excludes 0;
- `tables.html`, all numbers: for each liquidity tier (`ALL` first), every simulator fill model's fill-rate bias
  against the exact replay at every horizon with its 95% interval from the two-way dates x symbols bootstrap,
  sorted by the size of the bias at `--horizon`; the headline sentences (the most optimistic and pessimistic clear
  models, the closest, and with `pnl_by_tier` the largest clear P&L error among the 100 most traded symbols, ticks
  marked to the mid `--pnl-horizon` after the first fill); the best setting by tier; the flagged share of each row;
- `hftbacktest.html`, `nautilustrader.html`, `databento.html`: one library's answer, chart and box, then its models
  across tiers and horizons; `about.html`: how to read it, how it is made, its limits; `symbol.html`: the symbol
  lookup (the only page with a script: it reads `atlas/v1/symbols/<letter>.json` next to it);
- `atlas/v1/`: the versioned data API (`atlas_api.py`: `manifest.json`, `summary.json`, `configs.json`,
  `symbols/<letter>.json`; models in `queuetruth.atlas`), which `queuetruth atlas` reads;
- `data/leaderboard.json` and `data/atlas_manifest.json` as before, for programs;
- `symbols.html` with `--per-symbol`: per-symbol cross-day bias with empirical-Bayes shrinkage.

Two venues in one site: `--venue iex=ATLAS_DIR --venue nasdaq=ATLAS_DIR` (instead of `--atlas` and `--source`)
writes the first venue's pages at the top of `--out` and each other venue's pages, with their own `data/`, into
`<out>/<source>/`. Every venue has its own tables, headlines, roots, verify commands and attribution, and every page
links the other venues' index (no script: a link per venue). `.github/workflows/atlas.yml` puts IEX first, so the
IEX atlas is what the address shows by default. A day run on a symbol sample (`plan.py --sample-budget-core-h`) is
named on its venue's pages with the symbols it kept per tier.

With `--compat-link` the index links `compat.html`, the upstream compatibility page that
`scripts/nightly/compat_site.py` writes into the same directory (`.github/workflows/atlas.yml` does both).

The pages load nothing from another site; only `symbol.html` runs a script, which reads the data API next to it.
Output is byte-identical for the same inputs (no wall-clock values: the build information comes from the manifest,
and the "last built" date is `--built-at`, else SOURCE_DATE_EPOCH, else the newest day of the window). Every page is
readable on a 390 px phone and follows the system's dark mode. With `--source iex` every page carries IEX's
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
#: Plain names of the venues a site can show.
VENUES = {"iex": "IEX DEEP+", "nasdaq": "Nasdaq TotalView-ITCH"}
#: Where `.github/workflows/atlas.yml` keeps each venue's reduced days: (release tag, asset name prefix, docs anchor
#: of the verify steps).
VENUE_DAYS = {
    "iex": ("atlas-days", "day-", "verify-a-number"),
    "nasdaq": ("atlas-days-nasdaq", "nasdaq-day-", "verify-a-nasdaq-day"),
}
#: The Nasdaq sample days `scripts/realdata/itch_subset.py` pins (`itch_split.py --date` checks them against their
#: pinned hashes; `--url` would turn those checks off).
PINNED_ITCH = ("12302019", "01302020")
NASDAQ_SAMPLES_URL = "https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/"
#: Styles only a page with a venue switch needs, so a single-venue site stays byte-identical.
VENUE_CSS = "nav.venues { margin: 8px 0; }\n"
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
    """A difference the page may state as a finding: the metric's 95% interval excludes 0 on the side of its estimate
    (a percentile interval can lie wholly on the other side of a near-zero estimate; then nothing is stated, since
    "more" or "fewer" would contradict either the estimate or the interval)."""
    v, lo, hi = r.get(f"{metric}_value"), r.get(f"{metric}_ci_lo"), r.get(f"{metric}_ci_hi")
    if v is None or lo is None or hi is None:
        return False
    return (lo > 0 and v > 0) or (hi < 0 and v < 0)


def _named(model_id: str) -> str:
    """The plain label of a model in a sentence (`queuetruth.atlas._common.label`); the id stays in the tables and
    tooltips."""
    return _C().label(model_id)


def _C() -> Any:
    """`queuetruth.atlas._common`, imported on first use: this module stays stdlib-only at import time
    (`scripts/nightly/compat_site.py` imports it for its CSS and page frame)."""
    from queuetruth.atlas import _common

    return _common


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
REPO_HOME = DOCS.split("/blob/", 1)[0]


def commits(heads: list[str | None] | None) -> str:
    """Links to the commits a day (or the site) was built at, short shas; "not recorded" for none."""
    shas = [h for h in heads or [] if h]
    if not shas:
        return '<span class="muted">not recorded</span>'
    return ", ".join(
        f'<a href="{REPO_HOME}/commit/{html.escape(h)}"><code>{html.escape(h[:12])}</code></a>' for h in shas
    )


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
    """`hftbacktest Power3 n=2`: the compact label of a zoo model id; unknown families keep their id."""
    fam, params = parse_id(model_id)
    if fam not in SHORT:
        return model_id
    extra = " ".join(f"{k}={v}" for k, v in sorted(params.items()) if k not in _NOT_PARAMS)
    return f"{SHORT[fam]} {extra}".strip()


def _fnum(text: str | None) -> float | None:
    """A model parameter as a finite float, None when it is missing or not a number."""
    try:
        x = float(text) if text is not None else math.nan
    except ValueError:
        return None
    return x if math.isfinite(x) else None


#: NautilusTrader families -> `queue_position` on the venue (integrations/nautilus venue_options)
NT_QUEUE = {"nt_prob_touch": False, "nt_queue_l2_prob_touch": True, "nt_queue_l2": True}


def snippet(model_id: str) -> tuple[str, str] | None:
    """(where it goes, code) to reproduce a zoo model in its library; None when no library setting reproduces it
    (corrected mode, L3 input, Databento k outside (-1, 0], an unknown family, exchange or parameter).

    hftbacktest lines take the builder, exchange and number format from `queuetruth.export.hftbacktest`, so the site
    and `queuetruth export hftbacktest` cannot drift apart. The exchange is the export's default
    (`no_partial_fill_exchange`, which runs on the released hftbacktest 2.4.4: 2.4.4 stops on `partial_fill_exchange`,
    hftbacktest #312); an id whose `exch` is neither `partial` nor `no_partial` gets no snippet. The
    NautilusTrader line is the `add_venue` keywords the NautilusTrader integration sets."""
    from queuetruth.export._text import g6
    from queuetruth.export.hftbacktest import DEFAULT_EXCHANGE, EXCHANGES, FIXED_BUILDERS, POWER_BUILDERS

    fam, p = parse_id(model_id)
    if p.get("mode", "faithful") != "faithful":
        return None  # corrected mode is queuetruth's fixed variant: no released library does that
    if fam in NT_QUEUE:
        prob = _fnum(p.get("p", "1"))
        if prob is None or not 0 <= prob <= 1:
            return None
        return (
            "NautilusTrader BacktestEngine.add_venue keywords",
            f"fill_model=FillModel(prob_fill_on_limit={g6(prob)}), book_type=BookType.L2_MBP, trade_execution=True, "
            f"queue_position={NT_QUEUE[fam]}",
        )
    if p.get("exch", "partial").replace("-", "_") not in EXCHANGES:
        return None  # an exchange the libraries do not have
    exchange = EXCHANGES[DEFAULT_EXCHANGE]  # the one that runs on the released hftbacktest (#312, `hbt_note`)
    key = fam.removeprefix("hbt_")
    kind = "hftbacktest BacktestAsset chain"
    if fam.startswith("hbt_") and key in POWER_BUILDERS:
        n = _fnum(p.get("n"))
        if n is None:
            return None
        call = f"{POWER_BUILDERS[key]}({g6(n)})"
    elif fam.startswith("hbt_") and key in FIXED_BUILDERS:
        call = f"{FIXED_BUILDERS[key]}()"
    elif fam == "databento_mc":
        k = _fnum(p.get("k"))
        if k is None or not -1 < k <= 0:
            return None
        call = f"{POWER_BUILDERS['power3']}({g6(1 / (1 + k))})"
        kind = "hftbacktest approximation (expected value of the estimator)"
    else:
        return None
    return kind, f".{call}.{exchange}()"


def _bias(r: dict[str, Any] | None) -> float | None:
    v = None if r is None else r.get("fill_rate_bias_value")
    return v if v is not None and math.isfinite(v) else None


def side(r: dict[str, Any], metric: str = "fill_rate_bias") -> int:
    """+1 when the metric is clearly above 0, -1 clearly below, else 0, by `_clear` (the 95% interval excludes 0 on
    the side of the estimate). The words, the table colours and the chart colours all use it, so they never disagree;
    a skewed row (estimate and interval on opposite sides) is 0: grey, and not stated as a finding."""
    if not _clear(r, metric):
        return 0
    return 1 if r[f"{metric}_value"] > 0 else -1


def verdict(r: dict[str, Any]) -> str:
    """Whether a setting is still clearly off at its horizon: only a 95% interval that excludes 0 on the side of its
    estimate says so (`side`)."""
    s = side(r)
    if s:
        return "still clearly off: too optimistic" if s > 0 else "still clearly off: too pessimistic"
    return "no clear difference (95% interval includes 0)"


def _overlaps(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """The two settings' 95% intervals overlap (missing intervals count as overlapping): the atlas does not rank
    them."""
    alo, ahi = a.get("fill_rate_bias_ci_lo"), a.get("fill_rate_bias_ci_hi")
    blo, bhi = b.get("fill_rate_bias_ci_lo"), b.get("fill_rate_bias_ci_hi")
    if None in (alo, ahi, blo, bhi):
        return True
    return alo <= bhi and blo <= ahi


def _settings(rows: list[dict[str, Any]], tier: str, lib: str, horizon: int) -> list[dict[str, Any]]:
    """The library's settings in a tier at `horizon` (not its L3 model), closest point estimate first."""
    cands = [
        r
        for r in rows
        if r["tier"] == tier
        and r["horizon_ns"] == horizon
        and describe(r["model_id"])[0] == lib
        and parse_id(r["model_id"])[0] not in NOT_A_SETTING
        and _bias(r) is not None
    ]
    return sorted(cands, key=lambda r: (abs(r["fill_rate_bias_value"]), r["model_id"]))


def best_settings(rows: list[dict[str, Any]], horizon: int, libraries: tuple[str, ...] | None = None) -> list[dict]:
    """For each tier (`ALL` first) and library, the setting whose fill-rate bias at `horizon` is closest to 0 as a
    point estimate (ties by model id), whether its interval still excludes 0, the other settings whose intervals
    overlap its own (the atlas cannot rank those against it), and its snippet."""
    libs = libraries or tuple(LIBRARY_PAGES)
    out = []
    for tier in _tier_order({r["tier"] for r in rows}):
        for lib in libs:
            cands = _settings(rows, tier, lib, horizon)
            if not cands:
                continue
            r = cands[0]
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
                    "clearly_off": side(r) != 0,
                    "overlapping": [o["model_id"] for o in cands[1:] if _overlaps(r, o)],
                    "snippet_kind": snip[0] if snip else None,
                    "snippet": snip[1] if snip else None,
                }
            )
    return out


def _count(k: int, one: str, many: str) -> str:
    return f"{k} {one if k == 1 else many}"


def library_headline(rows: list[dict[str, Any]], lib: str, horizon: int) -> str | None:
    """A library page's fill-rate sentence (tier ALL, else the first tier): the exact fill rate, how many of the
    library's settings fill clearly more or fewer orders (95% interval excludes 0), the closest point estimate with
    whether it is still clearly off, and how many settings it cannot be ranked against (overlapping intervals)."""
    tiers = _tier_order({r["tier"] for r in rows})
    best = [b for b in best_settings(rows, horizon, (lib,)) if tiers and b["tier"] == tiers[0]]
    if not best:
        return None
    b = best[0]
    tier = b["tier"]
    cands = _settings(rows, tier, lib, horizon)
    exact = cands[0].get("exact_fill_rate_value")
    where = "" if tier == "ALL" else f" among {TIER_WORDS.get(tier, 'tier ' + tier)}"
    over = sum(1 for r in cands if side(r) > 0)
    under = sum(1 for r in cands if side(r) < 0)
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
    s = side(b)
    if s:
        tail = f"and it still fills clearly {'more' if s > 0 else 'fewer'} orders than the real queue"
    else:
        tail = "which the atlas cannot tell from the exact replay at this sample size"
    rank = ""
    if b["overlapping"]:
        k = len(b["overlapping"])
        rank = (
            f" Its interval overlaps {'that' if k == 1 else 'those'} of {_count(k, 'other setting', 'other settings')}"
            f" ({', '.join(_named(m) for m in b['overlapping'])}), so the atlas cannot rank them by closeness."
        )
    return (
        f"{lead}. The closest point estimate is {_named(b['model_id'])}, {pp(b['fill_rate_bias_value'])} pp (95% "
        f"interval {pp(b['fill_rate_bias_ci_lo'])} to {pp(b['fill_rate_bias_ci_hi'])} pp), {tail}.{rank}"
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
a { color: var(--fg); text-decoration-color: var(--muted); text-underline-offset: 2px; }
a:hover { text-decoration-color: var(--fg); } nav.libs { margin: 8px 0; } p { overflow-wrap: anywhere; }
table.best th, table.best td { text-align: left; white-space: normal; vertical-align: top; }
table.best td.cfg { min-width: 18em; } table.best td code { overflow-wrap: anywhere; }
figure.chart { margin: 8px 0 12px; } svg.bias { width: 100%; height: auto; display: block;
  font: 15px system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
svg.bias.narrow { display: none; max-width: 640px; }
@media (max-width: 760px) { svg.bias.wide { display: none; } svg.bias.narrow { display: block; } }
svg.bias text { fill: var(--fg); } svg.bias .tick { fill: var(--muted); } svg.bias .group { font-weight: 600; }
svg.bias .grid { stroke: var(--line); stroke-width: 1; } svg.bias .zero { stroke: var(--muted); stroke-width: 1.5; }
svg.bias .ci { stroke: var(--muted); stroke-width: 2; stroke-linecap: round; }
svg.bias .dot { fill: var(--muted); stroke: var(--bg); stroke-width: 2; }
svg.bias .over .ci { stroke: var(--over); } svg.bias .over .dot { fill: var(--over); }
svg.bias .under .ci { stroke: var(--under); } svg.bias .under .dot { fill: var(--under); }
"""


SUMMARY = "How far each backtester's fill model is from the exact replay of the real order queue."


def _page(title: str, body: str, attribution: str, description: str = SUMMARY, extra_css: str = "") -> str:
    """A whole page; `description` is the search and link-preview text (the headline when there is one). Other pages
    of the site (`scripts/nightly/compat_site.py`) pass `extra_css` for their own classes."""
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
        f"<title>{t}</title>\n{meta}<style>{CSS}{extra_css}</style>\n</head>\n<body>\n<main>\n"
        f"{body}\n{attribution}\n</main>\n</body>\n</html>\n"
    )


def _attribution(source: str) -> str:
    if source != "iex":
        return (
            '<p class="muted attr">Aggregate statistics computed from Nasdaq TotalView-ITCH 5.0 sample files '
            f'(<a href="{NASDAQ_SAMPLES_URL}">emi.nasdaq.com</a>). No market data is published here.</p>'
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


#: chart geometry in viewBox units, two layouts of the same chart (CSS shows one): `wide` puts each model on one
#: row (its label right-aligned in a column of its own, then its dot, whisker and value on the same baseline);
#: `narrow`, below 760 px, gives each model two lines (its label, then its dot and whisker across nearly the whole
#: width), so the chart reads on a 390 px phone without scrolling sideways
CHART_W, CHART_X0, CHART_X1, CHART_TOP, CHART_ROW, CHART_GROUP = 480, 22, 404, 34, 40, 26
#: where the narrow labels start (the plot leaves room for its first tick label)
CHART_LABEL_X = 4
#: the wide layout: width, right edge of the label column, plot, row and group heights
WIDE_W, WIDE_LABEL_END, WIDE_X0, WIDE_X1, WIDE_ROW, WIDE_GROUP = 1000, 430, 452, 920, 30, 30
#: longest label drawn (about 8 units a character at 15 px); the row's tooltip and the table keep the whole label
CHART_LABEL_CHARS = 58


def chart(rows: list[dict[str, Any]], chart_id: str, title: str, horizon: int, groups: bool = False) -> str:
    """Inline SVG of the fill-rate bias of `rows` (one per model, in the order given) at `horizon`: per model its
    plain label, a dot with its 95% interval as a whisker and its value, a line at 0, coloured `--over` / `--under`
    only when the interval excludes 0. Two layouts (`wide` and `narrow`, see CHART_W); CSS shows the one that fits.
    `groups` puts the library's name above each run of its models. No script; every value is also in a table of the
    site, and each row's tooltip has the model id. Empty without rows."""
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
    svgs = [
        _chart_svg(pts, ticks, step, (a, b), chart_id, title, horizon, groups, wide=True),
        _chart_svg(pts, ticks, step, (a, b), f"{chart_id}-n", title, horizon, groups, wide=False),
    ]
    return (
        '<figure class="chart">'
        + "".join(svgs)
        + (
            '<figcaption class="muted">Dot: the bias; whisker: its 95% interval. Coloured only when the interval '
            "excludes 0 (red: fills too many orders, blue: too few); grey: no clear difference. Bounds are in the "
            "tables, not drawn.</figcaption></figure>"
        )
    )


def _chart_svg(
    pts: list[tuple[dict[str, Any], float, float | None, float | None]],
    ticks: list[float],
    step: float,
    domain: tuple[float, float],
    chart_id: str,
    title: str,
    horizon: int,
    groups: bool,
    wide: bool,
) -> str:
    """One layout of `chart`."""
    C = _C()
    a, b = domain
    width, x0, x1 = (WIDE_W, WIDE_X0, WIDE_X1) if wide else (CHART_W, CHART_X0, CHART_X1)

    def x(v: float) -> float:
        return x0 + (v - a) / (b - a) * (x1 - x0)

    body: list[str] = []
    y = CHART_TOP
    last = None
    for r, v, lo, hi in pts:
        lib = C.library_of(r["model_id"])
        if groups and lib != last:
            gx, anchor = (WIDE_LABEL_END, ' text-anchor="end"') if wide else (CHART_LABEL_X, "")
            body.append(
                f'<text class="group" x="{gx}" y="{_f(y + 20 if wide else y + 18)}"{anchor}>'
                f"{html.escape(C.LIBRARY_NAMES[lib])}</text>"
            )
            y += WIDE_GROUP if wide else CHART_GROUP
        last = lib
        cls = {1: "over", -1: "under"}.get(side(r), "plain")
        ci = f" [{lo:+.2f}, {hi:+.2f}]" if lo is not None else ""
        label = C.label(r["model_id"])
        tip = f"{label} ({r['model_id']}): {v:+.2f} pp{ci}"
        body.append(f'<g class="{cls}"><title>{html.escape(tip)}</title>')
        shown = label if len(label) <= CHART_LABEL_CHARS else label[: CHART_LABEL_CHARS - 1].rstrip() + "\u2026"
        if wide:
            cy = y + WIDE_ROW / 2
            body.append(f'<text x="{WIDE_LABEL_END}" y="{_f(cy + 5)}" text-anchor="end">{html.escape(shown)}</text>')
        else:
            body.append(f'<text x="{CHART_LABEL_X}" y="{_f(y + 13)}">{html.escape(shown)}</text>')
            cy = y + 28
        if lo is not None:
            body.append(f'<line class="ci" x1="{_f(x(lo))}" x2="{_f(x(hi))}" y1="{_f(cy)}" y2="{_f(cy)}"/>')
        body.append(f'<circle class="dot" cx="{_f(x(v))}" cy="{_f(cy)}" r="5"/>')
        body.append(f'<text x="{width - 4}" y="{_f(cy + 5)}" text-anchor="end">{v:+.2f}</text></g>')
        y += WIDE_ROW if wide else CHART_ROW
    bottom = y
    height = bottom + 30
    out = [
        f'<svg class="bias {"wide" if wide else "narrow"}" viewBox="0 0 {width} {_f(height)}" role="img" '
        f'aria-labelledby="{chart_id}-t" aria-describedby="{chart_id}-d">',
        f'<title id="{chart_id}-t">{html.escape(title)}</title>',
        f'<desc id="{chart_id}-d">Fill-rate bias of {len(pts)} models at {html.escape(duration(horizon))} in '
        "percentage points, with 95% intervals; the same values are in the tables of the site.</desc>",
    ]
    for t in ticks:
        tx = _f(x(t))
        label = "0" if abs(t) < step / 1e6 else f"{round(t, 6):+g}"
        cls = "zero" if label == "0" else "grid"
        out.append(f'<line class="{cls}" x1="{tx}" x2="{tx}" y1="{CHART_TOP - 8}" y2="{_f(bottom)}"/>')
        out.append(f'<text class="tick" x="{tx}" y="{CHART_TOP - 14}" text-anchor="middle">{label}</text>')
    out += body
    out.append(
        f'<text class="tick" x="{_f((x0 + x1) / 2)}" y="{_f(height - 8)}" text-anchor="middle">'
        f"bias within {html.escape(duration(horizon))}, percentage points (model minus real queue)</text>"
    )
    out.append("</svg>")
    return "".join(out)


#: Where the hftbacktest L3 FIFO gap is explained (PR #26).
L3_FINDINGS = f"{REPO}/scripts/scale/findings/l3-fifo-gap.md"
#: One plain sentence on why hftbacktest's order-level model fills fewer orders (HTML), next to its chart and rows.
L3_NOTE = (
    "hftbacktest's L3 FIFO model reads the same order-level data and still fills fewer orders: that is hftbacktest's "
    "own behaviour. The released hftbacktest 2.4.4 gives the same first fills as the model here, and it misses fills "
    "of orders alone ahead of the displayed book: hidden executions it skips, which are frequent on IEX, and fills "
    "its trade-through rule refuses. The hidden-print share rests on queuetruth's ruling that displayed interest "
    f'fills before hidden at the same price (<a href="{L3_FINDINGS}">the findings</a>).'
)


def _row_note(model_id: str) -> str:
    """A short note under a model's name in the tables (hftbacktest's L3 FIFO model: why it fills fewer)."""
    if parse_id(model_id)[0] != "hbt_l3_fifo":
        return ""
    return f'<br><span class="ci">fills fewer by hftbacktest\'s own rules (<a href="{L3_FINDINGS}">why</a>)</span>'


def _chart_id(page_tier: str) -> str:
    return "bias-" + re.sub(r"[^A-Za-z0-9_-]", "-", page_tier)


def _cell(value: str, lo: str, hi: str, cls: str) -> str:
    return f'<td class="{cls}">{value}<br><span class="ci">[{lo}, {hi}]</span></td>'


def _cls(r: dict[str, Any], metric: str) -> str:
    return {1: "over", -1: "under"}.get(side(r, metric), "")


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
        lib, name = describe(m)[0], _named(m)
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
            f"<tr><td>{html.escape(lib)}</td><td>{html.escape(name)}<br><code>{html.escape(m)}</code>{_row_note(m)}</td>"
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


#: What goes with every NautilusTrader config (HTML).
NT_NOTE = (
    "<code>FillModel</code> is <code>nautilus_trader.backtest.models.FillModel</code> in 1.x and "
    "<code>nautilus_trader.execution.DefaultFillModel</code> in 2.x; the keywords go to "
    "<code>BacktestEngine.add_venue</code> (2.x also needs a <code>fee_model</code>). The faithful models follow "
    "nautilus_trader 1.231.0 on a delta-fed L2 book; from 2.0, a book update that shrinks the order's level also "
    "caps the queue ahead with <code>queue_position=True</code>, so a few orders fill differently. With "
    "<code>prob_fill_on_limit</code> below 1, every at-price print is a random draw: set <code>random_seed</code> "
    "on the fill model to compare runs."
)


#: What goes with every hftbacktest (and Databento-equivalent) config (HTML).
def hbt_note() -> str:
    """What goes with every hftbacktest (and Databento-equivalent) config (HTML): where the line goes, and the
    one-line #312 note on its exchange (`queuetruth.export.hftbacktest`, imported on first use)."""
    from queuetruth.export.hftbacktest import DEFAULT_EXCHANGE, EXCHANGES, HBT_312_URL

    return (
        "The hftbacktest line goes on your <code>BacktestAsset()</code> chain in place of its queue model and "
        "exchange (hftbacktest 2.4.4, which faithful mode reproduces). It uses "
        f"<code>{EXCHANGES[DEFAULT_EXCHANGE]}</code>, which runs on the released hftbacktest: 2.4.4 stops on "
        f"<code>{EXCHANGES['partial']}</code> with error 14 "
        f'(<a href="{HBT_312_URL}">hftbacktest #312</a>); the atlas ran these models with partial fills, and the '
        "two differ only when a fill is smaller than the order."
    )


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
            + f"<td>{html.escape(_named(b['model_id']))}<br><code>{html.escape(b['model_id'])}</code></td>"
            + _cell(pp(b["fill_rate_bias_value"]), pp(b["fill_rate_bias_ci_lo"]), pp(b["fill_rate_bias_ci_hi"]),
                    _cls(b, "fill_rate_bias"))
            + f"<td>{html.escape(verdict(b))}{_overlap_note(b)}</td>{pcell}"
            + f'<td class="cfg">{cfg}</td></tr>'
        )  # fmt: skip
    pnl_head = f"<th>P&amp;L, ticks ({html.escape(duration(ph))})</th>" if pnl_at is not None else ""
    libs = sorted({b["library"] for b in best})
    guides = ", ".join(f'<a href="{REPO}/{LIBRARY_PAGES[x][2]}">{html.escape(LIBRARY_PAGES[x][3])}</a>' for x in libs)
    nt = f" {NT_NOTE}" if "NautilusTrader" in libs else ""
    hbt = f" {hbt_note()}" if "hftbacktest" in libs or "Databento" in libs else ""
    return (
        f"<h2>{'Best setting by tier' if lib is None else f'Best {html.escape(lib)} setting by tier'}</h2>"
        f"<p>Which setting fixes it: for each tier{'' if lib else ' and library'}, the setting whose fill-rate bias "
        f"within {html.escape(duration(h))} is closest to 0 as a point estimate. It is <em>still clearly off</em> when "
        "its 95% interval excludes 0; otherwise the atlas cannot tell it from the exact replay at this sample size, "
        "which is not proof that it is exact. Settings whose intervals overlap the closest one's are listed under it: "
        "the atlas cannot rank those by closeness. hftbacktest's L3 FIFO model is left out: it needs order-level "
        "data.</p>"
        '<div class="wrap"><table class="best"><thead><tr><th>tier</th>'
        f"{'' if lib else '<th>library</th>'}<th>closest setting (point estimate)</th>"
        f"<th>bias ({html.escape(duration(h))})</th><th>verdict</th>{pnl_head}<th>config to paste</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
        f'<p class="muted">The setting is the one closest at this venue, horizon and population; refit on your own '
        f"data before relying on it ({guides}).{hbt}{nt}</p>"
    )


def _overlap_note(b: dict[str, Any]) -> str:
    if not b["overlapping"]:
        return ""
    names = ", ".join(_named(m) for m in b["overlapping"])
    return f'<br><span class="ci">interval overlaps: {html.escape(names)}</span>'


def venue_nav(venues: list[tuple[str, str]], here: str) -> str:
    """The venue switch: `venues` = [(source, href of its index from this page)], `here` the page's own venue."""
    links = [
        f'<span aria-current="page">{html.escape(VENUES[src])}</span>'
        if src == here
        else f'<a href="{html.escape(href)}">{html.escape(VENUES[src])}</a>'
        for src, href in venues
    ]
    return f'<nav class="venues">Venue: {" | ".join(links)}</nav>'


def sample_note(days: list[dict[str, Any]]) -> str:
    """Which days ran on a symbol sample and what each kept per tier; empty when every day ran every symbol."""
    said = []
    for d in days:
        ss = d.get("symbol_sample")
        if not ss:
            continue
        tiers = ", ".join(f"{html.escape(t)} {v['kept']:,} of {v['of']:,}" for t, v in ss["tiers"].items())
        said.append(f"{html.escape(iso_date(d['date']))}: {ss['per_tier']:,} symbols per tier ({tiers})")
    if not said:
        return ""
    return (
        '<p class="muted sample"><em>Symbol sample.</em> To fit one runner, these days ran a '
        "fixed sample of symbols in every liquidity tier, chosen by a hash of the symbol and the date, not by any "
        "result: "
        + "; ".join(said)
        + ". Tiers are ranked on the whole day. The ALL rows pool the sampled symbols, so each tier counts with its "
        "sample, not with its share of the market "
        f'(<a href="{DOCS}/how-to/run-the-atlas.md#a-nasdaq-day-on-one-runner">how the sample is made</a>).</p>'
    )


def recreate_command(source: str, day: dict[str, Any] | None) -> str:
    """The command that re-creates the buckets of `day` from the venue's public file (the input `verify_unit.py`
    re-runs a unit from); empty without a day."""
    if not day:
        return ""
    url = day.get("source_url") or "URL"
    k = day.get("buckets") or 32
    if source == "iex":
        cmd = f"scripts/scale/iex_split.py --url {url} --date {iso_date(day['date'])} --buckets {k} --out split"
    elif str(day["date"]) in PINNED_ITCH:
        cmd = f"scripts/scale/itch_split.py --date {day['date']} --buckets {k} --out split"
    else:
        cmd = f"scripts/scale/itch_split.py --url {url} --date {day['date']} --buckets {k} --out split"
    return (
        f"<p>The buckets of {html.escape(iso_date(day['date']))}, re-created from the public "
        f"{html.escape(VENUES[source])} file (streamed, never stored):</p>"
        f"<pre>uv run --frozen --no-sync python {html.escape(cmd)}</pre>"
    )


def _library_nav(present: list[str], here: str | None) -> str:
    links = [
        f'<span aria-current="page">{html.escape(lib)}</span>'
        if lib == here
        else f'<a href="{LIBRARY_PAGES[lib][0]}">{html.escape(lib)}</a>'
        for lib in present
    ]
    back = "" if here is None else '<a href="index.html">Fill-Realism Atlas</a> | '
    return f'<nav class="libs">{back}By library: {", ".join(links)}</nav>'


#: Site library names -> data API library keys (`queuetruth.atlas._common.LIBRARIES`).
LIBRARY_KEYS = {"hftbacktest": "hftbacktest", "NautilusTrader": "nautilus", "Databento": "databento"}
#: The atlas's public address (`.github/workflows/atlas.yml` deploys it with GitHub Pages).
SITE_URL = "https://sarptandoven.github.io/queuetruth/"

#: What a reader should know about each venue before trusting its numbers elsewhere (HTML).
SCOPE = {
    "iex": (
        "IEX carries about 4 to 5% of US equity volume. It delays every incoming order by a 350 microsecond speed "
        "bump and has order types that reprice themselves (discretionary and primary pegs), so its queues are "
        "thinner and behave differently from those of Nasdaq or NYSE. The size of a model's error may not transfer "
        "to other exchanges; the method does."
    ),
    "nasdaq": (
        "These are a few free Nasdaq TotalView-ITCH sample days, not a daily series, often on a fixed sample of "
        "symbols per liquidity tier. Read them next to the IEX pages, not instead of them."
    ),
}
#: Extra styles of the front page, the about page and the symbol lookup.
V2_CSS = """
p.sub { font-size: 1.05rem; margin: 0 0 12px; }
ul.answers { padding-left: 1.1em; } ul.answers li { margin: 6px 0; }
.boxes { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
section.box { border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }
section.box h3 { margin: 0 0 6px; font-size: 1.05rem; } section.box pre { white-space: pre-wrap;
  overflow-wrap: anywhere; } .scope { border-left: 3px solid var(--over); padding-left: 12px; }
details { margin: 8px 0; } summary { cursor: pointer; }
form.lookup { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 8px 0; }
form.lookup input { font: inherit; padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--bg); color: var(--fg); text-transform: uppercase; width: 9em; }
form.lookup button { font: inherit; padding: 6px 12px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--chip); color: var(--fg); cursor: pointer; }
footer.built { margin-top: 32px; }
"""


def _days_span(days: list[dict[str, Any]]) -> tuple[list[str], str]:
    dates = [iso_date(d["date"]) for d in days]
    if not dates:
        return dates, "no day"
    return dates, (dates[0] if len(dates) == 1 else f"{len(dates)} days from {dates[0]} to {dates[-1]}")


def _all_rows(atlas: Path, name: str) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    path = atlas / name
    return pq.read_table(path).to_pylist() if path.exists() else []


def _per_symbol_rows(atlas: Path, mode: str, population: str) -> list[dict[str, Any]]:
    """`per_symbol.parquet` rows of (mode, population) at the symbol shards' horizons, read with a filter (a
    20-day file holds millions of rows)."""
    import atlas_api as A
    import pyarrow.parquet as pq

    path = atlas / "per_symbol.parquet"
    if not path.exists():
        return []
    cols = ["mode", "shadow_pop", "model_id", "horizon_ns", "symbol", "tier", "n", "n_days", "bias", "ci_lo",
            "ci_hi", "bias_shrunk"]  # fmt: skip
    have = set(pq.read_schema(path).names)
    filters = [("mode", "=", mode), ("shadow_pop", "=", population), ("horizon_ns", "in", list(A.SYMBOL_HORIZONS))]
    return pq.read_table(path, columns=[c for c in cols if c in have], filters=filters).to_pylist()


def _plain(text: str) -> str:
    """A note without its markup, for the data API: links keep their address in brackets."""
    text = re.sub(r'<a href="([^"]+)">([^<]+)</a>', r"\2 (\1)", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _box(b: dict[str, Any], page: str | None, pnl_at: dict[tuple[str, str], dict[str, Any]] | None) -> str:
    """The "what to do" box of one library: its closest setting, the verdict and the config to paste."""
    C = _C()
    lib = b["library"]
    iv = {"value": 100 * b["fill_rate_bias_value"], "lo": None, "hi": None}
    if b["fill_rate_bias_ci_lo"] is not None and b["fill_rate_bias_ci_hi"] is not None:
        iv.update(lo=100 * b["fill_rate_bias_ci_lo"], hi=100 * b["fill_rate_bias_ci_hi"])
    v = C.verdict_key(iv)
    cfg = (
        f'<p class="muted">{html.escape(b["snippet_kind"])}:</p><pre><code>{html.escape(b["snippet"])}</code></pre>'
        if b["snippet"]
        else "<p>No released library setting reproduces it; see the guide.</p>"
    )
    note = NT_NOTE if lib == "NautilusTrader" else hbt_note() if b["snippet"] else ""
    pr = (pnl_at or {}).get((b["tier"], b["model_id"]))
    pnl = (
        f" Its P&amp;L error: {html.escape(C.interval_text(_ticks_iv(pr), 'ticks'))} per order."
        if pr and pr.get("pnl_bias_ticks_value") is not None
        else ""
    )
    overlap = (
        f" The atlas cannot rank it against {html.escape(', '.join(C.label(m) for m in b['overlapping']))} "
        "(overlapping intervals)."
        if b["overlapping"]
        else ""
    )
    more = f' <a href="{page}">Every tier and horizon</a>.' if page else ""
    return (
        f'<section class="box"><h3>{html.escape(lib)}</h3>'
        f"<p>Closest setting: <strong>{html.escape(C.label(b['model_id']))}</strong>, "
        f"{html.escape(C.interval_text(iv))}. Verdict: <strong>{html.escape(C.VERDICT_TEXT[v])}</strong>."
        f"{overlap}{pnl}</p>{cfg}"
        f"<details><summary>Model id and notes</summary><p><code>{html.escape(b['model_id'])}</code></p>"
        f'<p class="muted">{note} Refit on your own data before relying on it '
        f'(<a href="{REPO}/{LIBRARY_PAGES[lib][2]}">{html.escape(LIBRARY_PAGES[lib][3])}</a>).{more}</p></details>'
        "</section>"
    )


def _ticks_iv(pr: dict[str, Any] | None) -> dict[str, Any]:
    pr = pr or {}
    return {
        "value": pr.get("pnl_bias_ticks_value"),
        "lo": pr.get("pnl_bias_ticks_ci_lo"),
        "hi": pr.get("pnl_bias_ticks_ci_hi"),
    }


def _verify_section(
    source: str, manifest: dict[str, Any], days: list[dict[str, Any]], built_at: str, span: str, extra: str
) -> str:
    """Last built, the atlas root and how to verify it, every day root (folded), and where the data is."""
    release, prefix, anchor = VENUE_DAYS[source]
    root = manifest.get("atlas_root", "")
    day_rows = "".join(
        f"<tr><td>{html.escape(iso_date(d['date']))}</td><td><code>{html.escape(str(d['merkle_root']))}</code></td>"
        f"<td>{d.get('units_valid', '')} / {d.get('units_planned', '')}</td><td>{commits(d.get('git_heads'))}</td></tr>"
        for d in days
    )
    build = manifest.get("build") or {}
    return (
        '<footer class="built"><h2 id="verify">Last built and how to verify</h2>'
        f"<p>Last built <strong>{html.escape(built_at)}</strong>, from {html.escape(span)}, by "
        f'<a href="{REPO_HOME}">queuetruth</a> {html.escape(str(build.get("queuetruth_version") or ""))} '
        f"({commits([build.get('git_head')])}).</p>"
        f"<p>Atlas root <code>{html.escape(root)}</code>: a Merkle root over the day roots in date order (flag policy "
        f"<code>{html.escape(str(manifest.get('flag_policy')))}</code>). Each day root covers that day's shard leaves. "
        f"To check one, download the day (<code>{prefix}&lt;date&gt;.tar.gz</code> on the repository's "
        f'<a href="{REPO_HOME}/releases/tag/{release}">{release}</a> release: its tables, plan and manifest), check '
        "out the commit the day was built at, re-create the unit's bucket from the public day file and re-run the "
        f'unit (<a href="{DOCS}/concepts/fill-realism-atlas.md#{anchor}">the steps</a>):</p>'
        "<pre>uv run --frozen --no-sync python scripts/scale/verify_unit.py --day DAY_DIR "
        "--atlas-manifest atlas_manifest.json --unit-id UNIT_ID --split SPLIT_DIR</pre>"
        f"<details><summary>Every day root ({len(days)})</summary>"
        '<div class="wrap"><table><thead><tr><th>date</th><th>day root</th><th>units</th><th>built at</th></tr>'
        f"</thead><tbody>{day_rows}</tbody></table></div></details>"
        f"{extra}</footer>"
    )


def _check_list() -> str:
    return (
        "<h2>Check your own backtester</h2><ul>"
        "<li>This answer in your terminal, for your library, tier and symbol: <code>queuetruth atlas --library "
        "nautilus --symbol AAPL</code> (it asks before it downloads anything).</li>"
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
    compat_link: bool = False,
    venues: list[tuple[str, str]] | None = None,
    built_at: str | None = None,
) -> dict[str, Any]:
    """Writes the site; returns {pages, rows, pnl_rows, atlas_root, api}. `compat_link` links `compat.html`, the
    upstream compatibility page `scripts/nightly/compat_site.py` writes next to it. `venues` ([(source, href of its
    index)], `build_site`) adds the venue switch to every page and names the venue in the titles of all but the
    first. `built_at` is the "last built" date (default SOURCE_DATE_EPOCH, else the newest day)."""
    import atlas_api as A

    if source not in ("nasdaq", "iex"):
        raise SystemExit(f"site: source must be nasdaq or iex, got {source!r}")
    C = _C()
    manifest = json.loads((atlas / "atlas_manifest.json").read_text(encoding="utf-8"))
    venues_seen = {d["venue"] for d in manifest.get("days") or [] if d.get("venue")}
    if venues_seen - {source}:
        raise SystemExit(f"site: the atlas holds {sorted(venues_seen)} days, not {source} (attribution is per venue)")
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
    dates, span = _days_span(days)
    built = A.built_at(dates, built_at)
    attribution = _attribution(source)
    venue = VENUES[source]
    switch = venue_nav(venues, source) if venues else ""
    css = (VENUE_CSS if switch else "") + V2_CSS
    titled = f" ({venue})" if venues and venues[0][0] != source else ""
    present = [lib for lib in LIBRARY_PAGES if any(describe(r["model_id"])[0] == lib for r in rows)]
    best = best_settings(rows, h)
    hl = C.horizon_label(h)

    about = (
        f"{html.escape(venue)}, {html.escape(span)}, "
        f"{manifest.get('symbol_days', 0):,} symbol-days, {manifest.get('distinct_symbols', 0):,} symbols; "
        f"{html.escape(mode)} mode, shadow population {html.escape(population)} "
        '(<a href="about.html">how to read it, how it is made, its limits</a>).'
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
    note = sample_note(days)
    data_links = (
        ' Data: <a href="atlas/v1/manifest.json">data API</a> (<code>atlas/v1/</code>), '
        '<a href="data/leaderboard.json">leaderboard.json</a>, '
        '<a href="data/atlas_manifest.json">atlas_manifest.json</a>.'
    )
    more = (
        '<p class="muted">More: <a href="tables.html">all numbers</a> (every model, tier and horizon), '
        '<a href="about.html">how to read it, how it is made, its limits</a>, '
        '<a href="symbol.html">your symbol</a>'
        + (', <a href="symbols.html">every symbol</a>' if per_symbol else "")
        + "."
        + (
            ' Upstream compatibility: <a href="compat.html">the newest hftbacktest and NautilusTrader releases '
            "against the exact queue</a>, checked daily."
            if compat_link
            else ""
        )
        + data_links
        + "</p>"
    )
    recreate = recreate_command(source, days[-1] if days else None) if (switch or source != "iex") else ""

    # ---- tables.html: every model, tier and horizon (the atlas's first page until v2) ----------------------------
    tparts = [*([switch] if switch else []), '<p><a href="index.html">Fill-Realism Atlas</a></p>']
    tparts += [
        "<h1>All numbers</h1>",
        '<p class="muted">How far each backtester\'s fill model is from the exact replay of the real queue: '
        + about
        + "</p>",
    ]
    if note:
        tparts.append(note)
    if present:
        tparts.append(_library_nav(present, None))
    leads = [x for x in (headline(rows, h), pnl_headline(pnl, ph)) if x]
    for line in leads:
        tparts.append(f'<p class="lead">{html.escape(line, quote=False)}</p>')
    tparts.append(explain)
    tparts.append(_best_section(best, h, pnl_at, ph, None))
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
        tparts.append(page)
        shown += s
        shown_pnl += sp
    tparts.append(_verify_section(source, manifest, days, built, span, more + recreate))
    out.mkdir(parents=True, exist_ok=True)
    (out / "data").mkdir(exist_ok=True)
    (out / "tables.html").write_text(
        _page(f"All numbers: Fill-Realism Atlas{titled}", "\n".join(tparts), attribution,
              leads[0] if leads else SUMMARY, extra_css=css),
        encoding="utf-8",
    )  # fmt: skip

    # ---- the data API --------------------------------------------------------------------------------------------
    summary = A.summary_rows(_all_rows(atlas, "leaderboard_by_tier.parquet"), _all_rows(atlas, "pnl_by_tier.parquet"))
    configs = A.config_rows(best, pnl_at, LIBRARY_KEYS)
    notes = {
        "nautilus": _plain(NT_NOTE),
        "hftbacktest": _plain(hbt_note()),
        "databento": "Databento's estimator has no library of its own: the config is hftbacktest's expected-value "
        "equivalent. " + _plain(hbt_note()),
    }
    shards = A.symbol_shards(_per_symbol_rows(atlas, mode, population), mode, population)
    api = A.write_api(
        out,
        source=source,
        venue_name=venue,
        manifest=manifest,
        days=dates,
        summary=summary,
        configs=configs,
        notes=notes,
        shards=shards,
        mode=mode,
        population=population,
        horizon=hl,
        attribution=IEX_ATTRIBUTION if source == "iex" else None,
        verify=f"{DOCS}/concepts/fill-realism-atlas.md#{VENUE_DAYS[source][2]}",
        built=built,
    )

    # touch tables for bar backtests (scripts/scale/atlas_touch.py): a section and atlas/v1/touch.json when the atlas
    # has them, listed in the manifest with its sha256 like every other file of the API
    import atlas_touch

    touch = atlas_touch.section(atlas, out, population=population, source=source, docs=DOCS,
                                attribution=IEX_ATTRIBUTION if source == "iex" else None)  # fmt: skip
    if touch:
        api = A.add_file(out, "touch.json")

    # ---- index.html: the answer first ----------------------------------------------------------------------------
    first = tiers[0] if tiers else "ALL"
    here = [x for x in summary if x["mode"] == mode and x["shadow_population"] == population]
    at_h = [x for x in here if x["tier"] == first and x["horizon"] == hl]
    where = C.TIER_WORDS.get(first, first)
    parts = [*([switch] if switch else [])]
    parts += [
        "<h1>Fill-Realism Atlas</h1>",
        '<p class="sub">How far your backtester\'s fills are from the real order queue, measured by replaying '
        f"every order of {html.escape(venue)} exactly ({html.escape(span)}).</p>",
    ]
    if present:
        parts.append(_library_nav(present, None))
    ex = C.exact_answer(at_h, hl, where, venue)
    if ex:
        parts.append(f'<p class="lead">{html.escape(ex)}</p>')
    answers = []
    for lib in present:
        line = C.library_answer(at_h, LIBRARY_KEYS[lib])
        if line:
            answers.append(
                f'<li>{html.escape(line)} <a href="{LIBRARY_PAGES[lib][0]}">More on {html.escape(lib)}</a>.</li>'
            )
    if answers:
        parts.append(f'<ul class="answers">{"".join(answers)}</ul>')
    order = {lib: i for i, lib in enumerate(LIBRARY_PAGES)}
    drawn = sorted(
        (r for r in rows if r["tier"] == first and r["horizon_ns"] == h and describe(r["model_id"])[0] in order),
        key=lambda r: (order[describe(r["model_id"])[0]], -(_bias(r) or 0.0), r["model_id"]),
    )
    if drawn:
        title = f"Fill-rate bias within {C.horizon_words(hl)}, {where}"
        parts.append(f"<h2>{html.escape(title)}</h2>")
        parts.append(chart(drawn, "bias-front", title, h, groups=True))
    boxes = [_box(b, LIBRARY_PAGES[b["library"]][0], pnl_at) for b in best if b["tier"] == first]
    if boxes:
        parts.append(
            "<h2>What to do</h2><p>For each library, the setting closest to the real queue within "
            f"{html.escape(C.horizon_words(hl))} ({html.escape(where)}), the verdict and the config to paste. "
            '"No clear difference" means the atlas cannot tell it from the real queue at this sample size, not that '
            "it is exact.</p>"
            f'<div class="boxes">{"".join(boxes)}</div>'
        )
    if touch:  # the answer for bar-based backtests, after the libraries'
        parts.append(touch)
    parts.append(
        "<h2>Your symbol</h2>"
        '<form class="lookup" action="symbol.html" method="get" role="search">'
        '<label for="s">Symbol</label> <input id="s" name="s" autocomplete="off" spellcheck="false" '
        'maxlength="12" placeholder="AAPL"> <button type="submit">Look up</button></form>'
        '<p class="muted">Each library setting on one symbol at 1 s, 10 s and 1 min, with intervals and counts, from '
        '<a href="atlas/v1/manifest.json">the data API</a>.</p>'
    )
    parts.append(
        f'<h2>What this covers</h2><p class="scope">{SCOPE[source]} Small resting orders only: they do not move '
        'the market (<a href="about.html#limits">limits</a>).</p>'
    )
    if note:
        parts.append(note)
    parts.append(_check_list())
    parts.append(_verify_section(source, manifest, days, built, span, more + recreate))
    description = ex or SUMMARY
    (out / "index.html").write_text(
        _page(f"Fill-Realism Atlas{titled}", "\n".join(parts), attribution, description, extra_css=css),
        encoding="utf-8",
    )

    # ---- about.html and symbol.html ------------------------------------------------------------------------------
    (out / "about.html").write_text(
        _page(f"How to read the Fill-Realism Atlas{titled}", _about_body(switch, source, explain, h, ph, pnl),
              attribution, extra_css=css),
        encoding="utf-8",
    )  # fmt: skip
    (out / "symbol.html").write_text(
        _page(f"Your symbol: Fill-Realism Atlas{titled}", _symbol_body(switch, venue, span), attribution,
              "The Fill-Realism Atlas for one symbol.", extra_css=css + SYMBOL_CSS),
        encoding="utf-8",
    )  # fmt: skip

    data = {
        "schema": "qt.atlas_site_leaderboard",
        "schema_version": 1,
        "atlas_root": manifest.get("atlas_root", ""),
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
    pages = ["index.html", "tables.html", "about.html", "symbol.html"]
    built_line = (
        f'<p class="muted">Last built {html.escape(built)} by <a href="{REPO_HOME}">queuetruth</a> '
        f"{html.escape(str((manifest.get('build') or {}).get('queuetruth_version') or ''))} "
        f"({commits([(manifest.get('build') or {}).get('git_head')])}). Atlas root "
        f'<code>{html.escape(manifest.get("atlas_root", ""))}</code> (<a href="index.html#verify">how to verify '
        f"it</a>).{data_links}"
    )
    for lib in present:
        pages.append(
            _library_page(
                out, lib, rows, pnl, pnl_at, horizons, tiers, h, ph, best, present, about, explain + note,
                built_line, attribution, switch, titled, css,
            )
        )  # fmt: skip
    if per_symbol:
        pages.append(_symbols(atlas, out, mode, population, h, attribution))
    return {
        "pages": pages,
        "rows": len(shown),
        "pnl_rows": len(shown_pnl),
        "atlas_root": manifest.get("atlas_root", ""),
        "api": {"files": len(api["files"]) + 1, "symbol_shards": len(shards)},
    }


def _about_body(switch: str, source: str, explain: str, h: int, ph: int, pnl: list[dict[str, Any]]) -> str:
    """How to read the atlas, how it is made and its limits: the explanations the front page leaves out."""
    hw = html.escape(duration(h))
    pnl_item = (
        f"<li><strong>P&amp;L error</strong>, in ticks: the backtest's P&amp;L minus the exact replay's, per order "
        f"placed, each first fill marked to the mid {html.escape(duration(ph))} later. Positive: the backtest's "
        "P&amp;L is too high. It splits into fills the model grants that never happen, fills it misses, and fills "
        "it times differently.</li>"
        if pnl
        else ""
    )
    return "\n".join(
        [
            *([switch] if switch else []),
            '<p><a href="index.html">Fill-Realism Atlas</a></p>',
            "<h1>How to read the atlas</h1>",
            "<p>The atlas places small resting limit orders in the real order queue of every symbol of a public "
            "exchange day, replays every order of the day exactly to see which of them would have filled, and runs "
            "the fill models of popular backtesters on the same orders. It then says how far each model is from the "
            "real queue.</p>",
            "<h2>The numbers</h2><ul>"
            f"<li><strong>Fill-rate bias</strong>, in percentage points (pp): the share of orders the model fills "
            f"within a horizon (1 s to 5 min; the front page uses {hw}) minus the share the real queue fills. "
            "Positive: the backtest fills orders the real queue would not have filled, so it is too optimistic. "
            "+1.50 pp on a real fill rate of 12% means about 13% more fills than really happen.</li>"
            "<li><strong>95% interval</strong>: from a bootstrap that resamples trading days and symbols together. "
            "A difference is stated as a finding only when its interval excludes 0 on the side of its estimate; the "
            "colours follow the same rule (red: too many fills, blue: too few, grey: no clear difference).</li>"
            "<li><strong>Verdict</strong>: <em>still clearly off</em> when the interval of the closest setting "
            "excludes 0; <em>no clear difference</em> when it does not. That means the atlas cannot tell the setting "
            "from the real queue at this sample size, not that it is exact.</li>"
            f"{pnl_item}"
            "<li><strong>Tiers</strong>: symbols ranked by how much they trade (T1 the 100 most traded, then 101 to "
            "500, 501 to 1,500, 1,501 to 4,000, the rest). ALL pools them.</li>"
            "<li><strong>Bounds</strong>: two rules that bracket every honest model (always at the front of the "
            "queue; fill only when the price trades through). They are in the tables, not in the charts.</li></ul>",
            f"<h2>The tables</h2>{explain}",
            "<h2>How it is made</h2><ol>"
            "<li><strong>Data.</strong> One full public order-level day of the exchange, split by symbol (IEX DEEP+ "
            "is free to republish with its attribution).</li>"
            "<li><strong>Orders.</strong> On each symbol, shadow orders are placed at random times of the regular "
            "session, at the best price or one or two ticks from it, for one lot or the median order size.</li>"
            "<li><strong>Truth.</strong> The exact first-in, first-out replay of the order-level feed says when each "
            "order would have filled. Each symbol's replay is checked against the exchange's own trade prints.</li>"
            "<li><strong>Models.</strong> Every fill model runs on the same orders from the price-level (L2) view of "
            "the same book, as the libraries are released (faithful mode).</li>"
            "<li><strong>Statistics.</strong> Results are summed per symbol and day, and days are combined with the "
            "bootstrap above. Every day is kept with a Merkle root, so any number can be re-run and checked.</li></ol>",
            '<h2 id="limits">Its limits</h2><ul>'
            f"<li><strong>One venue.</strong> {SCOPE[source]}</li>"
            "<li><strong>Your orders do not move the market.</strong> A shadow order rests in the real queue without "
            "changing it: no market impact, no reaction by other traders.</li>"
            "<li><strong>Hidden liquidity, auctions and halts.</strong> Orders are placed during continuous trading; "
            "auction crosses are not modelled, and an order resting through a halt is not scored.</li>"
            "<li><strong>hftbacktest's order-level model sees the same queue but fills less.</strong> It never fills "
            "an order priced better than the feed's best by a trade-through when the executed order is at that best, "
            "which is how IEX publishes every execution, and it ignores hidden prints.</li>"
            + (
                "<li><strong>Markouts are not a strategy.</strong> The P&amp;L is the mid move after a fill, before "
                "fees and rebates.</li>"
                if pnl
                else ""
            )
            + "</ul>",
            "<h2>The data API</h2><p>The numbers of this site are also static JSON files under "
            '<a href="atlas/v1/manifest.json"><code>atlas/v1/</code></a>: <code>manifest.json</code> (window, days, '
            "atlas root, the sha256 of every other file), <code>summary.json</code> (every model, tier and horizon), "
            "<code>configs.json</code> (the closest setting per library and tier, with the config to paste) and "
            "<code>symbols/&lt;letter&gt;.json</code> (per symbol). <code>queuetruth atlas</code> reads them. "
            f'Details: <a href="{DOCS}/concepts/fill-realism-atlas.md">the Fill-Realism Atlas guide</a>.</p>',
        ]
    )


SYMBOL_CSS = """
#result table td:first-child { white-space: normal; min-width: 12em; }
#result .lib { font-weight: 600; text-align: left; padding-top: 14px; }
"""

#: The symbol lookup: reads `atlas/v1/symbols/<letter>.json` next to the page and fills a table with textContent
#: (never markup from the data). Vanilla JavaScript, nothing loaded from elsewhere.
SYMBOL_JS = r"""
(function () {
  var form = document.getElementById("lookup"), input = document.getElementById("s");
  var out = document.getElementById("result"), msg = document.getElementById("msg");
  var names = {hftbacktest: "hftbacktest", nautilus: "NautilusTrader", databento: "Databento"};
  function pp(x) { return x === null || x === undefined ? "n/a" : (x >= 0 ? "+" : "") + x.toFixed(2); }
  function cell(tr, text, cls) {
    var td = document.createElement("td"); td.textContent = text; if (cls) { td.className = cls; }
    tr.appendChild(td); return td;
  }
  function show(sym) {
    sym = (sym || "").trim().toUpperCase();
    out.textContent = ""; msg.textContent = "";
    if (!sym) { return; }
    input.value = sym;
    var c = sym.charAt(0), key = (c >= "A" && c <= "Z") ? c : "_";
    fetch("atlas/v1/symbols/" + key + ".json").then(function (r) {
      if (!r.ok) { throw new Error("no shard"); } return r.json();
    }).then(function (doc) {
      var e = null;
      for (var i = 0; i < doc.symbols.length; i++) { if (doc.symbols[i].symbol === sym) { e = doc.symbols[i]; } }
      if (!e) { msg.textContent = sym + " is not in the atlas (it may not trade on this venue)."; return; }
      msg.textContent = sym + ": tier " + e.tier + ", seen on " + e.days + " day" + (e.days === 1 ? "" : "s") +
        ". Bias in percentage points (model minus real queue) with its 95% interval and the orders behind it.";
      var t = document.createElement("table"), head = document.createElement("tr"), col = {};
      doc.columns.forEach(function (c, k) { col[c] = k; });
      ["setting"].concat(doc.horizons).forEach(function (h) {
        var th = document.createElement("th"); th.textContent = h; head.appendChild(th);
      });
      var thead = document.createElement("thead"); thead.appendChild(head); t.appendChild(thead);
      var body = document.createElement("tbody"), last = null;
      doc.settings.forEach(function (s, i) {
        var row = e.cells[i];
        if (!row) { return; }
        if (s.library !== last) {
          var g = document.createElement("tr"), td = cell(g, names[s.library] || s.library, "lib");
          td.colSpan = 1 + doc.horizons.length; body.appendChild(g); last = s.library;
        }
        var tr = document.createElement("tr"), name = cell(tr, s.label);
        name.title = s.model_id;
        doc.horizons.forEach(function (h, j) {
          var v = row[j];
          if (!v) { cell(tr, "n/a"); return; }
          var b = v[col.bias_pp], lo = v[col.lo_pp], hi = v[col.hi_pp];
          var cls = (lo !== null && lo > 0 && b > 0) ? "over" : ((hi !== null && hi < 0 && b < 0) ? "under" : "");
          var td = cell(tr, pp(b), cls), ci = document.createElement("span");
          ci.className = "ci";
          ci.textContent = (lo === null ? "one day: no interval" : "[" + pp(lo) + ", " + pp(hi) + "]") +
            " n=" + v[col.n].toLocaleString("en-US");
          td.appendChild(document.createElement("br")); td.appendChild(ci);
        });
        body.appendChild(tr);
      });
      t.appendChild(body);
      var wrap = document.createElement("div"); wrap.className = "wrap"; wrap.appendChild(t); out.appendChild(wrap);
    }).catch(function () {
      msg.textContent = "Could not read the data for " + sym + " (open the page from the site, not from a file).";
    });
  }
  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    history.replaceState(null, "", "?s=" + encodeURIComponent(input.value.trim().toUpperCase()));
    show(input.value);
  });
  var q = new URLSearchParams(window.location.search).get("s");
  if (q) { show(q); }
})();
"""


def _symbol_body(switch: str, venue: str, span: str) -> str:
    return "\n".join(
        [
            *([switch] if switch else []),
            '<p><a href="index.html">Fill-Realism Atlas</a></p>',
            "<h1>Your symbol</h1>",
            f'<p class="sub">Every library setting on one symbol of {html.escape(venue)} ({html.escape(span)}), '
            "within 1 s, 10 s and 1 min.</p>",
            '<form class="lookup" id="lookup" role="search"><label for="s">Symbol</label> '
            '<input id="s" name="s" autocomplete="off" spellcheck="false" maxlength="12" placeholder="AAPL"> '
            '<button type="submit">Look up</button></form>',
            '<p id="msg" class="muted" aria-live="polite"></p><div id="result"></div>',
            "<noscript><p>The lookup needs JavaScript. The same numbers are in "
            "<code>atlas/v1/symbols/&lt;first letter&gt;.json</code>, and <code>queuetruth atlas --symbol "
            "SYMBOL</code> prints them.</p></noscript>",
            '<p class="muted">A symbol seen on one day has no interval. Fewer orders on a symbol mean wider intervals: '
            'read the tier on <a href="tables.html">all numbers</a> for a steadier picture.</p>',
            f"<script>{SYMBOL_JS}</script>",
        ]
    )


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
    switch: str = "",
    titled: str = "",
    css: str = "",
) -> str:
    """`hftbacktest.html` and the like: one library's answer in plain words, its chart, what to do, then its models
    across tiers and horizons (fill rate and P&L) and its best setting by tier; titled with the question people
    search for."""
    import atlas_api as A

    C = _C()
    name, question, guide, guide_name = LIBRARY_PAGES[lib]
    mine = [r for r in rows if describe(r["model_id"])[0] == lib]
    my_pnl = [r for r in pnl if describe(r["model_id"])[0] == lib]
    first = tiers[0] if tiers else "ALL"
    hl = C.horizon_label(h)
    api_rows = [x for x in A.summary_rows([r for r in mine if r["tier"] == first and r["horizon_ns"] == h], [])]
    answer = C.library_answer(api_rows, LIBRARY_KEYS[lib])
    where = C.TIER_WORDS.get(first, first)
    detail = [x for x in (library_headline(mine, lib, h), pnl_headline(my_pnl, ph, f"{lib}'s models")) if x]
    drawn = sorted(
        (r for r in mine if r["tier"] == first and r["horizon_ns"] == h),
        key=lambda r: (-(_bias(r) or 0.0), r["model_id"]),
    )
    title = f"{lib}: fill-rate bias within {C.horizon_words(hl)}, {where}"
    boxes = [_box(b, None, pnl_at) for b in best if b["library"] == lib and b["tier"] == first]
    parts = [
        *([switch] if switch else []),
        _library_nav(present, lib),
        f"<h1>{html.escape(question)}</h1>",
        *([f'<p class="lead">{html.escape(answer)}</p>'] if answer else []),
        f"<h2>{html.escape(title)}</h2>",
        chart(drawn, "bias-lib", title, h),
        *(
            [f'<p class="muted">{L3_NOTE}</p>']
            if any(parse_id(r["model_id"])[0] == "hbt_l3_fifo" for r in drawn)
            else []
        ),
        *(["<h2>What to do</h2>", *boxes] if boxes else []),
        "<h2>Details</h2>",
        f'<p class="muted">{html.escape(lib)}\'s fill models against the exact replay of the real queue: {about}</p>',
        *(f"<p>{html.escape(line, quote=False)}</p>" for line in detail),
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
        f"<li>In your terminal: <code>queuetruth atlas --library {LIBRARY_KEYS[lib]}</code>.</li>"
        f'<li>What each model does: <a href="{DOCS}/concepts/l2-model-zoo.md">the L2 model zoo</a>.</li>'
        f'<li>Every library side by side: <a href="index.html">the Fill-Realism Atlas</a>.</li></ul>' + built + "</p>"
    )
    page_title = f"{question} Fill-Realism Atlas{titled}"
    page = _page(page_title, "\n".join(parts), attribution, answer or SUMMARY, extra_css=css)
    (out / name).write_text(page, encoding="utf-8")
    return name


def _symbols(atlas: Path, out: Path, mode: str, population: str, h: int, attribution: str) -> str:
    import pyarrow.parquet as pq

    filters = [("mode", "=", mode), ("shadow_pop", "=", population), ("horizon_ns", "=", h)]
    per = pq.read_table(atlas / "per_symbol.parquet", filters=filters).to_pylist()
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


def build_site(atlases: list[tuple[str, Path]], out: Path, *, compat_link: bool = False, **kw: Any) -> dict:
    """One site over several venues: `atlases` = [(source, combine dir)], the first at the top of `out` (the default
    venue), each other in `out/<source>/`; every page links every venue's index. `compat_link` goes to the first
    venue's index only. Returns {venues: {source: what `build` returned, with its `dir`}}."""
    sources = [src for src, _ in atlases]
    if not atlases or len(set(sources)) != len(sources) or any(src not in VENUES for src in sources):
        raise SystemExit(f"site: give each venue once, from {sorted(VENUES)} (got {sources})")
    first = sources[0]
    res = {}
    for i, (src, atlas) in enumerate(atlases):
        up = "../" if i else ""  # the other venues sit one directory down from the first
        links = [(s, f"{up}index.html" if s == first else f"{up}{s}/index.html") for s in sources]
        switch = links if len(sources) > 1 else None  # one venue: the single-venue site, no switch
        r = build(atlas, out / src if i else out, source=src, compat_link=compat_link and i == 0, venues=switch, **kw)
        res[src] = {**r, "dir": f"{src}/" if i else "."}
    return {"venues": res}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--atlas", type=Path, help="an `atlas.py combine` output directory (one venue)")
    ap.add_argument(
        "--venue",
        action="append",
        default=[],
        metavar="SOURCE=ATLAS_DIR",
        help="one venue of a multi-venue site (repeat; the first is the default venue at the top of --out)",
    )
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--source", choices=("nasdaq", "iex"), default="nasdaq")
    ap.add_argument("--mode", default="faithful")
    ap.add_argument("--population", help="shadow population of the plan (default standard, else the first)")
    ap.add_argument(
        "--horizon", help="sort and headline horizon, e.g. 1s, 100ms, 1min (default 60 s, else the longest)"
    )
    ap.add_argument("--per-symbol", action="store_true", help="also write symbols.html from per_symbol.parquet")
    ap.add_argument("--pnl-horizon", default="60s", help="markout horizon of the P&L column (atlases with P&L)")
    ap.add_argument(
        "--compat-link", action="store_true", help="link compat.html (scripts/nightly/compat_site.py) from the index"
    )
    ap.add_argument(
        "--built-at",
        help="the 'last built' date or time on every page and in the data API (default "
        "SOURCE_DATE_EPOCH, else the newest day of the window)",
    )
    a = ap.parse_args(argv)
    if bool(a.atlas) == bool(a.venue):
        ap.error("give --atlas (one venue) or --venue SOURCE=ATLAS_DIR (one or more), not both")
    kw = dict(
        mode=a.mode,
        population=a.population,
        horizon=a.horizon,
        per_symbol=a.per_symbol,
        pnl_horizon=a.pnl_horizon,
        compat_link=a.compat_link,
        built_at=a.built_at,
    )
    if a.venue:
        pairs = []
        for v in a.venue:
            src, sep, path = v.partition("=")
            if not sep or not path:
                ap.error(f"--venue {v!r}: expected SOURCE=ATLAS_DIR")
            pairs.append((src, Path(path)))
        r = build_site(pairs, a.out, **kw)
    else:
        r = build(a.atlas, a.out, source=a.source, **kw)
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
