"""The atlas data API (`atlas/v1/`), written next to the site by `atlas_site.py`.

    <site>/atlas/v1/manifest.json        qt.atlas_api_manifest: window, days, atlas root, sha256 of every other file
    <site>/atlas/v1/summary.json         qt.atlas_summary: every model, mode, population, tier and horizon
    <site>/atlas/v1/configs.json         qt.atlas_configs: per library and tier, the closest setting and its config
    <site>/atlas/v1/symbols/<A-Z|_>.json qt.atlas_symbols: per symbol, every library setting at 1 s, 10 s and 1 min

The models are in `queuetruth.atlas` (one module per file); `queuetruth atlas` and `qt.atlas.lookup` read these
files. Units are human: percent, percentage points, ticks, horizons as `1s` / `1min`. Output is byte-identical for
the same inputs: no wall-clock value unless `built_at` is given (or SOURCE_DATE_EPOCH is set).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from queuetruth.atlas import _common as C

API_DIR = Path("atlas") / "v1"
#: horizons of the symbol shards (those the atlas has)
SYMBOL_HORIZONS = (1_000_000_000, 10_000_000_000, 60_000_000_000)


def _num(x: Any, scale: float = 1.0, digits: int = 4) -> float | None:
    """A finite number times `scale`, rounded (so the files do not carry float noise); None otherwise."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v * scale, digits) if math.isfinite(v) else None


def _iv(r: dict[str, Any] | None, metric: str, scale: float = 100.0, digits: int = 4) -> dict[str, Any]:
    r = r or {}
    return {
        "value": _num(r.get(f"{metric}_value"), scale, digits),
        "lo": _num(r.get(f"{metric}_ci_lo"), scale, digits),
        "hi": _num(r.get(f"{metric}_ci_hi"), scale, digits),
    }


def built_at(days: list[str], given: str | None = None) -> str:
    """`given`, else SOURCE_DATE_EPOCH as an ISO time, else the newest day of the window (ISO date)."""
    if given:
        return given
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch and epoch.strip().isdigit():
        return datetime.fromtimestamp(int(epoch), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return days[-1] if days else ""


def summary_rows(rows: list[dict[str, Any]], pnl: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`qt.atlas_summary` rows from `leaderboard_by_tier` rows (every mode and population) and `pnl_by_tier` rows
    (joined where the P&L markout horizon equals the row's horizon)."""
    pnl_at = {(r["mode"], r["shadow_pop"], r["tier"], r["model_id"], r["horizon_ns"]): r for r in pnl}
    out = []
    for r in rows:
        lib = C.library_of(r["model_id"])
        fam = C.parse_id(r["model_id"])[0]
        bias = _iv(r, "fill_rate_bias")
        pr = pnl_at.get((r["mode"], r["shadow_pop"], r["tier"], r["model_id"], r["horizon_ns"]))
        pnl_iv = _iv(pr, "pnl_bias_ticks", 1.0, 5) if pr and pr.get("pnl_bias_ticks_value") is not None else None
        out.append(
            {
                "library": lib,
                "library_name": C.LIBRARY_NAMES[lib],
                "label": C.label(r["model_id"]),
                "model_id": r["model_id"],
                "is_setting": lib in C.LIBRARIES and fam not in C.NOT_A_SETTING,
                "mode": r["mode"],
                "shadow_population": r["shadow_pop"],
                "tier": r["tier"],
                "tier_name": C.TIER_WORDS.get(r["tier"], r["tier"]),
                "horizon": C.horizon_label(int(r["horizon_ns"])),
                "horizon_seconds": int(r["horizon_ns"]) / 1e9,
                "fill_rate_bias_pp": bias,
                "exact_fill_rate_pct": _iv(r, "exact_fill_rate"),
                "model_fill_rate_pct": _num(r.get("model_fill_rate_value"), 100.0),
                "verdict": C.verdict_key(bias),
                "n": int(r.get("n") or 0),
                "n_symbols": r.get("n_symbols"),
                "n_dates": r.get("n_dates"),
                "pnl_bias_ticks": pnl_iv,
                "flagged_share": _num(r.get("flagged_share"), 1.0, 6),
            }
        )
    order = {lib: i for i, lib in enumerate((*C.LIBRARIES, "bound", "other"))}
    tiers = {t: i for i, t in enumerate(C.TIER_WORDS)}
    out.sort(
        key=lambda x: (
            x["mode"],
            x["shadow_population"],
            tiers.get(x["tier"], 99),
            x["tier"],
            order[x["library"]],
            x["model_id"],
            x["horizon_seconds"],
        )
    )
    return out


def config_rows(
    best: list[dict[str, Any]], pnl_at: dict[tuple[str, str], dict[str, Any]] | None, library_key: dict[str, str]
) -> list[dict[str, Any]]:
    """`qt.atlas_configs` entries from `atlas_site.best_settings` (fractions) and the P&L rows at the site's P&L
    horizon; `library_key` maps the site's library names to API keys."""
    out = []
    for b in best:
        lib = library_key[b["library"]]
        iv = {
            "value": _num(b["fill_rate_bias_value"], 100.0),
            "lo": _num(b["fill_rate_bias_ci_lo"], 100.0),
            "hi": _num(b["fill_rate_bias_ci_hi"], 100.0),
        }
        pr = (pnl_at or {}).get((b["tier"], b["model_id"]))
        v = C.verdict_key(iv)
        out.append(
            {
                "library": lib,
                "library_name": C.LIBRARY_NAMES[lib],
                "tier": b["tier"],
                "tier_name": C.TIER_WORDS.get(b["tier"], b["tier"]),
                "label": C.label(b["model_id"]),
                "model_id": b["model_id"],
                "n_settings": b["n_settings"],
                "fill_rate_bias_pp": iv,
                "verdict": v,
                "verdict_text": C.VERDICT_TEXT[v],
                "overlapping": list(b["overlapping"]),
                "overlapping_labels": [C.label(m) for m in b["overlapping"]],
                "pnl_bias_ticks": _iv(pr, "pnl_bias_ticks", 1.0, 5)
                if pr and pr.get("pnl_bias_ticks_value") is not None
                else None,
                "config": b["snippet"],
                "config_kind": b["snippet_kind"],
            }
        )
    return out


def letter(symbol: str) -> str:
    """The shard of a symbol: its first letter, upper case; `_` for any other first character."""
    c = symbol[:1].upper()
    return c if "A" <= c <= "Z" else "_"


def symbol_shards(per: list[dict[str, Any]], mode: str, population: str) -> dict[str, dict[str, Any]]:
    """{letter: {horizons, settings, symbols}} from `per_symbol.parquet` rows: the library settings (no bounds, not
    the L3 model) in (mode, population) at the 1 s, 10 s and 1 min horizons the atlas has, in the compact layout of
    `queuetruth.atlas.symbols` (pp rounded to 0.01, the precision every page prints)."""
    keep = [
        r
        for r in per
        if r.get("mode") == mode
        and r.get("shadow_pop") == population
        and int(r.get("horizon_ns") or 0) in SYMBOL_HORIZONS
        and C.library_of(r["model_id"]) in C.LIBRARIES
        and C.parse_id(r["model_id"])[0] not in C.NOT_A_SETTING
    ]
    order = {lib: i for i, lib in enumerate(C.LIBRARIES)}
    hz = sorted({int(r["horizon_ns"]) for r in keep})
    hpos = {h: j for j, h in enumerate(hz)}
    by_letter: dict[str, dict[str, dict[str, Any]]] = {}
    for r in keep:
        sym = r["symbol"]
        e = by_letter.setdefault(letter(sym), {}).setdefault(
            sym, {"symbol": sym, "tier": r.get("tier") or "", "days": 0, "m": {}}
        )
        e["days"] = max(e["days"], int(r.get("n_days") or 0))
        e["m"].setdefault(r["model_id"], {})[hpos[int(r["horizon_ns"])]] = [
            _num(r.get("bias"), 100.0, 2),
            _num(r.get("ci_lo"), 100.0, 2),
            _num(r.get("ci_hi"), 100.0, 2),
            _num(r.get("bias_shrunk"), 100.0, 2),
            int(r.get("n") or 0),
            int(r.get("n_days") or 0),
        ]
    shards: dict[str, dict[str, Any]] = {}
    for key in sorted(by_letter):
        syms = by_letter[key]
        models = sorted({m for e in syms.values() for m in e["m"]}, key=lambda m: (order[C.library_of(m)], m))
        entries = []
        for sym in sorted(syms):
            e = syms[sym]
            cells = [
                None if m not in e["m"] else [e["m"][m].get(j) for j in range(len(hz))] for m in models
            ]  # fmt: skip
            entries.append({"symbol": sym, "tier": e["tier"], "days": e["days"], "cells": cells})
        shards[key] = {
            "horizons": [C.horizon_label(h) for h in hz],
            "settings": [{"library": C.library_of(m), "label": C.label(m), "model_id": m} for m in models],
            "symbols": entries,
        }
    return shards


def _dump(doc: Any) -> bytes:
    data = doc.model_dump(mode="json", by_alias=True)
    return (json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def write_api(
    site: Path,
    *,
    source: str,
    venue_name: str,
    manifest: dict[str, Any],
    days: list[str],
    summary: list[dict[str, Any]],
    configs: list[dict[str, Any]],
    notes: dict[str, str],
    shards: dict[str, dict[str, Any]],
    mode: str,
    population: str,
    horizon: str,
    attribution: str | None,
    verify: str,
    built: str,
) -> dict[str, Any]:
    """Writes `<site>/atlas/v1/` and returns the manifest as a dict. Every document is validated by its model."""
    from queuetruth.atlas.configs import AtlasConfigs
    from queuetruth.atlas.manifest import AtlasApiManifest
    from queuetruth.atlas.summary import AtlasSummary
    from queuetruth.atlas.symbols import AtlasSymbols

    root = str(manifest.get("atlas_root") or "")
    base = site / API_DIR
    if base.exists():  # a rebuild never keeps a shard of a symbol that left the window
        for p in sorted(base.rglob("*.json"), reverse=True):
            p.unlink()
    (base / "symbols").mkdir(parents=True, exist_ok=True)
    files: dict[str, bytes] = {
        "summary.json": _dump(
            AtlasSummary(venue=source, atlas_root=root, headline_horizon=horizon, rows=summary)  # type: ignore[arg-type]
        ),
        "configs.json": _dump(
            AtlasConfigs(
                venue=source,
                atlas_root=root,
                mode=mode,
                shadow_population=population,
                horizon=horizon,
                notes=notes,
                configs=configs,  # type: ignore[arg-type]
            )
        ),
    }
    for key in sorted(shards):
        files[f"symbols/{key}.json"] = _dump(
            AtlasSymbols(
                venue=source,
                atlas_root=root,
                letter=key,
                mode=mode,
                shadow_population=population,
                **shards[key],
            )
        )
    entries = []
    for path in sorted(files):
        data = files[path]
        (base / path).write_bytes(data)
        entries.append({"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    m = AtlasApiManifest(
        built_at=built,
        atlas_root=root,
        venue=source,
        venue_name=venue_name,
        window={"from": days[0] if days else "", "to": days[-1] if days else ""},  # type: ignore[arg-type]
        days=days,
        symbol_days=int(manifest.get("symbol_days") or 0),
        symbols=int(manifest.get("distinct_symbols") or 0),
        mode=mode,
        shadow_population=population,
        headline_horizon=horizon,
        attribution=attribution,
        verify=verify,
        files=entries,  # type: ignore[arg-type]
    )
    data = json.dumps(m.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=1) + "\n"
    (base / "manifest.json").write_text(data, encoding="utf-8")
    return m.model_dump(mode="json", by_alias=True)


__all__ = ["API_DIR", "built_at", "config_rows", "letter", "summary_rows", "symbol_shards", "write_api"]
