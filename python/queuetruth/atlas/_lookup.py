"""Reading the atlas data API and answering from it: `qt.atlas.lookup(...)` and `queuetruth atlas`.

Where the data comes from:

- `source=DIR`: a directory with the API (`DIR/manifest.json`, or a site directory with `DIR/atlas/v1/`). Nothing is
  downloaded.
- otherwise the public site (`DEFAULT_URL`, or `source=URL`), through a cache under the user cache directory
  (`queuetruth.demo.cache.user_cache_dir()/atlas/<host and path>`). Nothing is downloaded without consent, as for
  `queuetruth demo`: `yes=True` (`--yes`), or `confirm` (the CLI asks on a terminal) says yes. A cached copy is used
  offline; `refresh=True` downloads the manifest again. Every file is checked against the sha256 the manifest lists
  before it is used or cached.

Every file is validated by its model (`queuetruth.atlas.summary` and the others), so an answer never rests on a file
the API does not define.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from queuetruth.atlas import _common as C
from queuetruth.errors import QtError

#: The data API of the public atlas (`.github/workflows/atlas.yml` deploys the site with GitHub Pages).
DEFAULT_URL = "https://sarptandoven.github.io/queuetruth/atlas/v1/"
#: Largest API file read (a symbol shard of a 20-day atlas is about 2 MB).
MAX_BYTES = 64 << 20


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load(data: bytes, what: str) -> dict[str, Any]:
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise QtError("QT-INPUT-002", f"atlas: {what} is not JSON: {e}") from e
    if not isinstance(doc, dict):
        raise QtError("QT-INPUT-002", f"atlas: {what} is not a JSON object")
    return doc


def _validate(doc: dict[str, Any], what: str) -> dict[str, Any]:
    """The document checked against its model; returns it as given."""
    from pydantic import ValidationError

    from queuetruth.atlas._registry import doc_models

    model = doc_models().get(str(doc.get("schema")))
    if model is None:
        raise QtError("QT-INPUT-002", f"atlas: {what} is not an atlas API document (schema {doc.get('schema')!r})")
    try:
        model.model_validate(doc)
    except ValidationError as e:
        raise QtError("QT-INPUT-002", f"atlas: {what} does not match {model.SCHEMA}: {e.errors()[0]['msg']}") from e
    return doc


class Source:
    """The files of one copy of the API; `get(path)` returns a document checked against the manifest's sha256."""

    where: str
    manifest: dict[str, Any]

    def _raw(self, path: str) -> bytes:  # pragma: no cover - overridden
        raise NotImplementedError

    def has(self, path: str) -> bool:
        return any(f["path"] == path for f in self.manifest["files"])

    def get(self, path: str) -> dict[str, Any]:
        entry = next((f for f in self.manifest["files"] if f["path"] == path), None)
        if entry is None:
            raise QtError("QT-INPUT-001", f"atlas: {path} is not in the manifest of {self.where}")
        data = self._raw(path)
        if _sha(data) != entry["sha256"]:
            raise QtError(
                "QT-INPUT-002",
                f"atlas: {path} of {self.where} does not match the sha256 in its manifest",
                "the copy is damaged or from another build: refresh it (--refresh) or rebuild the site",
            )
        return _validate(_load(data, path), path)


class LocalSource(Source):
    def __init__(self, path: Path) -> None:
        p = Path(path).expanduser()
        base = p / "atlas" / "v1" if (p / "atlas" / "v1" / "manifest.json").is_file() else p
        if not (base / "manifest.json").is_file():
            raise QtError(
                "QT-INPUT-001",
                f"atlas: no manifest.json in {p} (nor in {p / 'atlas' / 'v1'})",
                "point --from at a site directory or its atlas/v1 directory",
            )
        self.base = base
        self.where = str(base)
        self.manifest = _validate(_load((base / "manifest.json").read_bytes(), "manifest.json"), "manifest.json")

    def _raw(self, path: str) -> bytes:
        p = self.base / path
        if not p.is_file():
            raise QtError("QT-INPUT-001", f"atlas: {p} is missing")
        return p.read_bytes()


def _cache_dir(url: str) -> Path:
    from queuetruth.demo.cache import user_cache_dir

    u = urllib.parse.urlsplit(url)
    parts = [x for x in (u.netloc + u.path).split("/") if x and x not in (".", "..")]
    return user_cache_dir() / "atlas" / Path(*parts)


def _get(url: str, timeout_s: float = 30.0) -> bytes:
    """One HTTPS GET of at most MAX_BYTES (`QT-INPUT-001` on any network error)."""
    from queuetruth._version import __version__

    req = urllib.request.Request(url, headers={"User-Agent": f"queuetruth/{__version__} (atlas)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:  # https only, checked by the caller
            data = r.read(MAX_BYTES + 1)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise QtError("QT-INPUT-001", f"atlas: could not download {url}: {e}", "check the connection") from e
    if len(data) > MAX_BYTES:
        raise QtError("QT-INPUT-002", f"atlas: {url} is larger than {MAX_BYTES >> 20} MB")
    return data


class RemoteSource(Source):
    """The API at `url`, through the cache; downloads only after `consent()` returns True."""

    def __init__(
        self,
        url: str,
        consent: Callable[[str], bool],
        refresh: bool = False,
        fetch: Callable[[str], bytes] | None = None,
        cache: Path | None = None,
    ) -> None:
        if not url.startswith("https://"):
            raise QtError("QT-USAGE-001", f"atlas: {url} is not an https:// URL or a directory")
        self.url = url if url.endswith("/") else url + "/"
        self.cache = cache or _cache_dir(self.url)
        self.where = f"{self.url} (cached in {self.cache})"
        self._consent = consent
        self._asked: bool | None = None
        self._fetch = fetch or _get
        self.downloaded: list[str] = []
        cached = self.cache / "manifest.json"
        if cached.is_file() and not refresh:
            self.manifest = _validate(_load(cached.read_bytes(), "the cached manifest.json"), "manifest.json")
            self.offline = True
            return
        data = self._download("manifest.json")
        self.manifest = _validate(_load(data, "manifest.json"), "manifest.json")
        self.offline = False
        self.cache.mkdir(parents=True, exist_ok=True)
        # a new manifest: the cached files of the old one are dropped, so a stale file is never read with it
        for p in sorted(self.cache.rglob("*.json"), reverse=True):
            p.unlink()
        self._store("manifest.json", data)

    def _download(self, path: str) -> bytes:
        if self._asked is None:
            self._asked = bool(self._consent(f"{self.url}{path}"))
        if not self._asked:
            raise QtError(
                "QT-USAGE-001",
                f"atlas: the answer needs files from {self.url} and nothing was downloaded",
                "rerun with --yes (Python: yes=True) to download them, or --from DIR for a local copy",
            )
        self.downloaded.append(path)
        return self._fetch(f"{self.url}{path}")

    def _store(self, path: str, data: bytes) -> None:
        p = self.cache / path
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(p)

    def _raw(self, path: str) -> bytes:
        entry = next(f for f in self.manifest["files"] if f["path"] == path)
        p = self.cache / path
        if p.is_file():
            data = p.read_bytes()
            if _sha(data) == entry["sha256"]:
                return data
        data = self._download(path)
        if _sha(data) != entry["sha256"]:
            raise QtError(
                "QT-INPUT-002",
                f"atlas: {self.url}{path} does not match the sha256 in its manifest",
                "the site may be mid-deploy: rerun with --refresh in a minute",
            )
        self._store(path, data)
        return data


def open_source(
    source: str | Path | None = None,
    *,
    yes: bool = False,
    confirm: Callable[[str], bool] | None = None,
    refresh: bool = False,
    fetch: Callable[[str], bytes] | None = None,
) -> Source:
    """A local directory, or the API at a URL (default: the public atlas) through the cache."""
    if source is not None and not str(source).startswith(("http://", "https://")):
        return LocalSource(Path(source))

    def consent(what: str) -> bool:
        if yes:
            return True
        return bool(confirm(what)) if confirm is not None else False

    return RemoteSource(str(source or DEFAULT_URL), consent, refresh=refresh, fetch=fetch)


def _row(x: dict[str, Any], days: int | None) -> dict[str, Any]:
    return {
        "library": x["library"],
        "label": x["label"],
        "model_id": x["model_id"],
        "horizon": x["horizon"],
        "fill_rate_bias_pp": x["fill_rate_bias_pp"],
        "verdict": C.verdict_key(x["fill_rate_bias_pp"]),
        "n": x["n"],
        "days": days,
    }


class AtlasResult:
    """The answer of `lookup`: `.summary` (the answer in plain words), `.rows` and `.configs` (lists of dicts),
    `.table()` (a pandas DataFrame), `.doc` (the `qt.atlas_answer` document) and `.text()` (what the CLI prints)."""

    def __init__(self, doc: dict[str, Any]) -> None:
        from queuetruth.atlas.answer import AtlasAnswer

        AtlasAnswer.model_validate(doc)
        self.doc = doc

    @property
    def summary(self) -> str:
        return str(self.doc["summary"])

    @property
    def rows(self) -> list[dict[str, Any]]:
        return list(self.doc["rows"])

    @property
    def configs(self) -> list[dict[str, Any]]:
        return list(self.doc["configs"])

    def table(self) -> Any:
        """The rows as a pandas DataFrame: library, setting, horizon, bias in pp with its interval, verdict, n."""
        import pandas as pd

        return pd.DataFrame(
            [
                {
                    "library": C.LIBRARY_NAMES[r["library"]],
                    "setting": r["label"],
                    "horizon": r["horizon"],
                    "bias_pp": r["fill_rate_bias_pp"]["value"],
                    "ci_lo_pp": r["fill_rate_bias_pp"]["lo"],
                    "ci_hi_pp": r["fill_rate_bias_pp"]["hi"],
                    "verdict": r["verdict"],
                    "n": r["n"],
                    "days": r["days"],
                    "model_id": r["model_id"],
                }
                for r in self.rows
            ]
        )

    def text(self) -> str:
        return render(self.doc)

    def __str__(self) -> str:
        return self.text()

    def __repr__(self) -> str:
        return f"AtlasResult({self.summary!r})"


def lookup(
    library: str | None = None,
    symbol: str | None = None,
    tier: str | None = None,
    horizon: str | None = None,
    source: str | Path | None = None,
    *,
    yes: bool = False,
    refresh: bool = False,
    confirm: Callable[[str], bool] | None = None,
    fetch: Callable[[str], bytes] | None = None,
) -> AtlasResult:
    """The atlas answer for `library` (`hftbacktest`, `nautilus`, `databento`; all three by default), one `symbol` or
    a liquidity `tier` (default `ALL`, or the symbol's tier), at `horizon` (default the site's, `1min`): the answer in
    plain words, every setting's bias with its 95% interval, and the closest setting's config to paste.

    `source` is a directory or a URL of the data API (default: the public atlas, cached under the user cache
    directory). Nothing is downloaded unless `yes=True` or `confirm(url)` returns True."""
    if library is not None and library not in C.LIBRARIES:
        raise QtError("QT-USAGE-001", f"atlas: unknown library {library!r}", f"use one of {', '.join(C.LIBRARIES)}")
    src = open_source(source, yes=yes, confirm=confirm, refresh=refresh, fetch=fetch)
    m = src.manifest
    hl = m["headline_horizon"] if horizon is None else _horizon(horizon)
    libs = [library] if library else list(C.LIBRARIES)
    warnings: list[str] = []
    summary_doc = src.get("summary.json")
    here = [
        x for x in summary_doc["rows"] if x["mode"] == m["mode"] and x["shadow_population"] == m["shadow_population"]
    ]
    tiers = sorted({x["tier"] for x in here}, key=lambda t: list(C.TIER_WORDS).index(t) if t in C.TIER_WORDS else 99)
    sym = symbol.strip().upper() if symbol else None
    rows: list[dict[str, Any]] = []
    lines: list[str] = []
    entry = None
    if sym:
        key = _letter(sym)
        empty: dict[str, Any] = {"symbols": [], "horizons": [], "settings": []}
        shard = src.get(f"symbols/{key}.json") if src.has(f"symbols/{key}.json") else empty
        entry = next((e for e in shard["symbols"] if e["symbol"] == sym), None)
        if entry is None:
            raise QtError(
                "QT-USAGE-001",
                f"atlas: {sym} is not in the atlas ({m['venue_name']}, {m['window']['from']} to {m['window']['to']})",
                "it may not trade on this venue, or not on enough days; try --tier instead",
            )
        if hl not in shard["horizons"]:
            raise QtError(
                "QT-USAGE-001",
                f"atlas: per symbol the atlas has {', '.join(shard['horizons'])}, not {hl}",
                f"use --horizon {shard['horizons'][-1]}",
            )
        if tier and tier != entry["tier"]:
            warnings.append(f"{sym} is in tier {entry['tier']}, not {tier}: the configs are those of {entry['tier']}")
        tier = entry["tier"]
        col = {c: k for k, c in enumerate(shard["columns"])}
        at_h = []
        for st, cells in zip(shard["settings"], entry["cells"], strict=True):
            if st["library"] not in libs or cells is None:
                continue
            for h, c in zip(shard["horizons"], cells, strict=True):
                if c is None:
                    continue
                iv = {"value": c[col["bias_pp"]], "lo": c[col["lo_pp"]], "hi": c[col["hi_pp"]]}
                x = {**st, "horizon": h, "fill_rate_bias_pp": iv, "n": int(c[col["n"]] or 0)}
                rows.append(_row(x, int(c[col["n_days"]] or 0)))
                if h == hl:
                    at_h.append({**x, "exact_fill_rate_pct": None})
        days = entry["days"]
        lines.append(
            f"{sym} on {m['venue_name']} ({C.TIER_WORDS.get(tier, tier)}, tier {tier}; {days} day"
            f"{'' if days == 1 else 's'}), within {C.horizon_words(hl)}:"
        )
        for lib in libs:
            said = C.library_answer(at_h, lib)
            if said:
                lines.append(said)
        if not at_h:
            lines.append(f"No setting has a number on {sym} within {C.horizon_words(hl)}; see the table.")
        if any(r["fill_rate_bias_pp"]["lo"] is None for r in rows):
            warnings.append(f"{sym} was seen on one day: its numbers have no interval")
    else:
        tier = tier or (tiers[0] if tiers else "ALL")
        if tier not in tiers:
            raise QtError("QT-USAGE-001", f"atlas: no tier {tier!r}", f"use one of {', '.join(tiers)}")
        at_tier = [x for x in here if x["tier"] == tier]
        have = sorted({x["horizon"] for x in at_tier}, key=C.parse_horizon)
        if hl not in have:
            raise QtError("QT-USAGE-001", f"atlas: no horizon {hl}", f"use one of {', '.join(have)}")
        at_h = [x for x in at_tier if x["horizon"] == hl and x["library"] in libs]
        rows = [_row(x, x.get("n_dates")) for x in sorted(at_h, key=_order) if x["is_setting"] or _l3(x)]
        ex = C.exact_answer(at_h, hl, C.TIER_WORDS.get(tier, tier), m["venue_name"])
        if ex:
            lines.append(ex)
        for lib in libs:
            said = C.library_answer(at_h, lib)
            if said:
                lines.append(said)
    if not rows:
        raise QtError("QT-USAGE-001", "atlas: no setting of that library in the atlas for this choice")
    configs_doc = src.get("configs.json")
    cfgs = []
    for c in configs_doc["configs"]:
        if c["tier"] == tier and c["library"] in libs:
            cfgs.append(
                {
                    "library": c["library"],
                    "label": c["label"],
                    "model_id": c["model_id"],
                    "fill_rate_bias_pp": c["fill_rate_bias_pp"],
                    "verdict": c["verdict"],
                    "verdict_text": c["verdict_text"],
                    "config": c["config"],
                    "config_kind": c["config_kind"],
                    "note": configs_doc["notes"].get(c["library"]),
                }
            )
    if cfgs and configs_doc["horizon"] != hl:
        warnings.append(f"the configs are the closest settings within {configs_doc['horizon']}, the site's horizon")
    if sym and cfgs:
        warnings.append(f"the configs are those of {sym}'s tier ({tier}): a single symbol is too noisy to pick one")
    w = m["window"]
    doc = {
        "schema": "qt.atlas_answer",
        "schema_version": 1,
        "summary": "\n".join(lines),
        "venue": m["venue"],
        "venue_name": m["venue_name"],
        "atlas_root": m["atlas_root"],
        "built_at": m["built_at"],
        "window": f"{len(m['days'])} day{'' if len(m['days']) == 1 else 's'} from {w['from']} to {w['to']}",
        "library": library,
        "symbol": sym,
        "tier": tier,
        "tier_name": C.TIER_WORDS.get(tier, tier),
        "horizon": hl,
        "mode": m["mode"],
        "shadow_population": m["shadow_population"],
        "source": src.where,
        "rows": rows,
        "configs": cfgs,
        "attribution": m.get("attribution"),
        "warnings": warnings,
    }
    return AtlasResult(doc)


def _l3(x: dict[str, Any]) -> bool:
    """hftbacktest's order-level model: not a setting of an L2 backtest, but part of hftbacktest's answer."""
    return C.parse_id(x["model_id"])[0] in C.NOT_A_SETTING


def _order(x: dict[str, Any]) -> tuple:
    return (C.LIBRARIES.index(x["library"]), -(x["fill_rate_bias_pp"]["value"] or 0.0), x["model_id"])


def _horizon(text: str) -> str:
    try:
        return C.horizon_label(C.parse_horizon(text))
    except ValueError as e:
        raise QtError("QT-USAGE-001", f"atlas: {e}") from e


def _letter(symbol: str) -> str:
    c = symbol[:1].upper()
    return c if "A" <= c <= "Z" else "_"


def render(doc: dict[str, Any]) -> str:
    """The human text of a `qt.atlas_answer`: the answer, the table, the config to paste, the source."""
    out = [doc["summary"], ""]
    hz = sorted({r["horizon"] for r in doc["rows"]}, key=C.parse_horizon)
    by: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for r in doc["rows"]:
        by.setdefault((r["library"], r["label"]), {})[r["horizon"]] = r

    def cell(r: dict[str, Any] | None) -> str:
        if r is None:
            return "n/a"
        iv = r["fill_rate_bias_pp"]
        ci = f"[{C.pp(iv['lo'])}, {C.pp(iv['hi'])}]" if iv["lo"] is not None else "[no interval]"
        return f"{C.pp(iv['value'])} {ci} n={r['n']:,}"

    table = [(k, [cell(v.get(h)) for h in hz]) for k, v in by.items()]
    lw = max(len("setting"), *(len(k[1]) + 2 for k, _ in table))
    cw = [max(len(h), *(len(c[i]) for _, c in table)) for i, h in enumerate(hz)]
    out.append("setting".ljust(lw) + "".join("   " + h.rjust(w) for h, w in zip(hz, cw, strict=True)))
    last = None
    for k, cells in table:
        if k[0] != last:
            out.append(f"{C.LIBRARY_NAMES[k[0]]}:")
            last = k[0]
        out.append(("  " + k[1]).ljust(lw) + "".join("   " + c.rjust(w) for c, w in zip(cells, cw, strict=True)))
    out.append("bias in percentage points (model minus real queue), 95% interval; n: orders")
    for c in doc["configs"]:
        out.append("")
        where = f"{doc['tier_name']}, within {C.horizon_words(doc['horizon'])}"
        out.append(f"{C.LIBRARY_NAMES[c['library']]}: closest setting ({where}): {c['label']}")
        out.append(f"  {C.interval_text(c['fill_rate_bias_pp'])}; {c['verdict_text']}")
        if c["config"]:
            out.append(f"  config to paste ({c['config_kind']}):")
            out.append(f"    {c['config']}")
        else:
            out.append(f"  no released library setting reproduces it (model id {c['model_id']})")
    out.append("")
    out.append(
        f"Source: Fill-Realism Atlas, {doc['venue_name']}, {doc['window']}, built {doc['built_at']}, atlas root "
        f"{doc['atlas_root'][:16]}... ({doc['source']})"
    )
    out.append("Refit on your own data before relying on a setting: the atlas is one venue and small orders.")
    if doc.get("attribution"):
        out.append(doc["attribution"])
    return "\n".join(out)


__all__ = ["DEFAULT_URL", "AtlasResult", "LocalSource", "RemoteSource", "lookup", "open_source", "render"]
