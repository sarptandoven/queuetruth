"""`qt.atlas.calibration(library, tier=None, symbol=None)`: the atlas's closest setting of a library, as the same
`FitResult` that `qt.fit(...)` returns, so one line in each framework takes either:

    import queuetruth as qt
    cal = qt.atlas.calibration("nautilus", tier="T1_top100", yes=True)   # or qt.fit("nautilus", data=...)
    engine.add_venue(..., **qtn.venue_options(cal))                       # import queuetruth_nautilus as qtn
    asset = qthbt.configure(asset, qt.atlas.calibration("hftbacktest"))  # import queuetruth_hftbacktest as qthbt

It reads `configs.json` of the atlas data API through the same consent-based fetch and cache as `queuetruth atlas`
(`queuetruth.atlas._lookup.open_source`): nothing is downloaded unless `yes=True` or `confirm(url)` says yes. A
`symbol` resolves to its liquidity tier through the symbol shards; the setting is the tier's (one symbol is too
noisy to pick one).

The `qt.fit` document it carries has `source: "atlas"`, the setting in the library's units (`param`), the settings to
apply (`settings`), where it came from (`atlas`: root, venue, window, days, tier, horizon, the setting's bias with its
95% interval) and a verdict. The verdict passes when a released library setting reproduces the atlas model; whether
that setting is still clearly off is said in the answer and in `atlas.verdict`, never hidden. It is not a fit on your
data: the answer says so, and `qt.fit` on your own data is the next step.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from queuetruth.atlas import _common as C
from queuetruth.errors import QtError

#: Libraries with a one-line setting (the `qt.fit` backtesters).
LIBRARIES = ("nautilus", "hftbacktest", "databento")
#: Atlas model family -> the `qt.fit` family name.
FAMILY = {
    "nt_prob_touch": "nt_prob_touch",
    "nt_queue_l2_prob_touch": "nt_queue_l2_prob_touch",
    "nt_queue_l2": "nt_queue_l2_prob_touch",  # queue_position=True at prob_fill_on_limit=1: the same settings
    "hbt_power": "power",
    "hbt_power2": "power2",
    "hbt_power3": "power3",
    "hbt_log": "log",
    "hbt_log2": "log2",
    "hbt_risk_adverse": "risk_adverse",
    "databento_mc": "databento",
}


def _tier_of(src: Any, symbol: str) -> tuple[str, int]:
    """(tier, days) of `symbol` from its symbol shard."""
    from queuetruth.atlas._lookup import _letter

    m = src.manifest
    key = _letter(symbol)
    shard = src.get(f"symbols/{key}.json") if src.has(f"symbols/{key}.json") else {"symbols": []}
    entry = next((e for e in shard["symbols"] if e["symbol"] == symbol), None)
    if entry is None:
        raise QtError(
            "QT-USAGE-001",
            f"atlas: {symbol} is not in the atlas ({m['venue_name']}, {m['window']['from']} to {m['window']['to']})",
            "it may not trade on this venue, or not on enough days; pass tier= instead",
        )
    return str(entry["tier"]), int(entry["days"])


def tier_key(tier: str | None, tiers: list[str]) -> str | None:
    """A tier as the atlas names it: exact (`T1_top100`), or its short name (`T1`, any case) when that is unique."""
    if tier is None or tier in tiers:
        return tier
    short = [t for t in tiers if t.split("_", 1)[0].upper() == tier.strip().upper()]
    return short[0] if len(short) == 1 else tier


def _value(fam: str, params: dict[str, str]) -> float | None:
    """The setting's number in the library's units: `p` (NautilusTrader), `n` (hftbacktest power), `k` (Databento)."""
    key = {"nautilus": "p"}.get(C.FAMILY_LIBRARY.get(fam, ""), "n" if fam.startswith("hbt_power") else "k")
    text = params.get(key, "1" if key == "p" else None)
    try:
        return None if text is None else float(text)
    except ValueError:
        return None


def calibration(
    library: str,
    tier: str | None = None,
    symbol: str | None = None,
    source: str | Path | None = None,
    *,
    yes: bool = False,
    refresh: bool = False,
    confirm: Callable[[str], bool] | None = None,
    fetch: Callable[[str], bytes] | None = None,
) -> Any:
    """The atlas's closest setting of `library` (`nautilus`, `hftbacktest`, `databento`) in a liquidity `tier` (default
    `ALL`; `T1_top100` or its short name `T1`) or the tier of `symbol`, as a `queuetruth.fit.FitResult` whose source
    is `atlas`.

    `source` is a directory or a URL of the data API (default: the public atlas, cached under the user cache
    directory). Nothing is downloaded unless `yes=True` or `confirm(url)` returns True."""
    from queuetruth.atlas._lookup import open_source
    from queuetruth.fit.run import FitResult, settings_of

    if library not in LIBRARIES:
        raise QtError(
            "QT-USAGE-001", f"atlas: no one-line setting for {library!r}", f"use one of {', '.join(LIBRARIES)}"
        )
    src = open_source(source, yes=yes, confirm=confirm, refresh=refresh, fetch=fetch)
    m = src.manifest
    sym = symbol.strip().upper() if symbol else None
    doc_cfg = src.get("configs.json")
    have = [c for c in doc_cfg["configs"] if c["library"] == library]
    tiers = list(dict.fromkeys(c["tier"] for c in have))
    tier = tier_key(tier, tiers)
    warnings: list[str] = []
    days_seen = None
    if sym:
        sym_tier, days_seen = _tier_of(src, sym)
        if tier and tier != sym_tier:
            warnings.append(f"{sym} is in tier {sym_tier}, not {tier}: the setting is that of {sym_tier}")
        tier = sym_tier
        warnings.append(f"the setting is that of {sym}'s tier ({tier}): a single symbol is too noisy to pick one")
    tier = tier or ("ALL" if "ALL" in tiers else (tiers[0] if tiers else "ALL"))
    cfg = next((c for c in have if c["tier"] == tier), None)
    if cfg is None:
        raise QtError(
            "QT-USAGE-001",
            f"atlas: no {C.LIBRARY_NAMES[library]} setting for tier {tier!r}",
            f"use one of {', '.join(tiers)}" if tiers else "this atlas has no setting of that library",
        )
    afam, params = C.parse_id(cfg["model_id"])
    fam = FAMILY.get(afam, afam)
    value = _value(afam, params)
    settings = settings_of(library, fam, value) if cfg["config"] is not None else {}
    reasons = []
    if not settings:
        reasons.append(
            {
                "code": "no_library_setting",
                "text": f"no released {C.LIBRARY_NAMES[library]} setting reproduces the atlas model {cfg['model_id']}",
            }
        )
    from queuetruth.score.calibrate_live import DEFAULTS, PARAM

    w = m["window"]
    n_days = len(m["days"])
    atlas = {
        "atlas_root": m["atlas_root"],
        "source": src.where,
        "venue": m["venue"],
        "venue_name": m["venue_name"],
        "built_at": m["built_at"],
        "window": f"{n_days} day{'' if n_days == 1 else 's'} from {w['from']} to {w['to']}",
        "days": n_days,
        "mode": doc_cfg["mode"],
        "shadow_population": doc_cfg["shadow_population"],
        "tier": tier,
        "tier_name": C.TIER_WORDS.get(tier, tier),
        "symbol": sym,
        "symbol_days": days_seen,
        "horizon": doc_cfg["horizon"],
        "model_id": cfg["model_id"],
        "label": cfg["label"],
        "n_settings": cfg["n_settings"],
        "fill_rate_bias_pp": cfg["fill_rate_bias_pp"],
        "verdict": cfg["verdict"],
        "verdict_text": cfg["verdict_text"],
        "overlapping_labels": list(cfg["overlapping_labels"]),
        "config": cfg["config"],
        "config_kind": cfg["config_kind"],
        "note": doc_cfg["notes"].get(library),
        "attribution": m.get("attribution"),
    }
    from queuetruth.fit.run import SETTING

    doc: dict[str, Any] = {
        "schema": "qt.fit",
        "answer": "",
        "backtester": library,
        "family": fam,
        "source": "atlas",
        "command": "atlas",
        "run_id": None,
        "param": {
            "name": PARAM.get(fam),
            "setting": SETTING.get(fam, cfg["label"]),
            "value": value if PARAM.get(fam) else None,
            "ci": None,
            "ci_kind": None,
            "default": DEFAULTS.get(fam),
            "n_events": 0,
        },
        "split": None,
        "held_out": [],
        "verdict": {"passed": not reasons, "reasons": reasons, "remedies": [] if not reasons else [_REFIT]},
        "snippet": None
        if cfg["config"] is None
        else {"path": None, "text": cfg["config"], "notes": [n for n in [atlas["note"]] if n], "one_liner": None},
        "settings": settings,
        "atlas": atlas,
        "warnings": warnings,
        "files": {"fit": None, "calibration": None, "shadows": None, "snippet": None},
    }
    from queuetruth.fit.render import answer

    doc["answer"] = answer(doc)
    return FitResult(doc, None, None)


_REFIT = "Fit on your own data: qt.fit(library, data=...) on order-level data of your venue, or --live with your fills."

__all__ = ["LIBRARIES", "calibration", "tier_key"]
