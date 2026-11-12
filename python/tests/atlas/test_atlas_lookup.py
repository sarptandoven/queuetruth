"""`qt.atlas.lookup` and `queuetruth atlas` on a synthetic atlas data API (invented numbers, no market data): the
answer in plain words, the table and the config, from a local directory or a URL through the cache, with consent and
sha256 checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import queuetruth as qt
from queuetruth import cli
from queuetruth.atlas import _common as C
from queuetruth.atlas import _lookup as L
from queuetruth.errors import QtError

TOUCH = "nt_prob_touch[mode=faithful;p=1]"
QPOS = "nt_queue_l2[mode=faithful]"
PB2 = "hbt_power2[exch=partial;mode=faithful;n=2]"
L3 = "hbt_l3_fifo[mode=faithful]"
FRONT = "front_of_queue[mode=faithful]"
#: tier -> model -> (bias, lo, hi) in pp at 1min
BIAS = {
    "ALL": {TOUCH: (1.5, 1.2, 1.8), QPOS: (0.06, -0.1, 0.2), PB2: (-0.8, -1.0, -0.6), L3: (-1.8, -2.0, -1.6),
            FRONT: (12.0, 11.0, 13.0)},
    "T1_top100": {TOUCH: (1.9, 1.5, 2.3), QPOS: (-0.3, -0.5, -0.1), PB2: (-0.5, -0.9, -0.1), L3: (-1.0, -1.4, -0.6),
                  FRONT: (10.0, 9.0, 11.0)},
}  # fmt: skip


def _summary_row(tier: str, model: str, horizon: str, b: tuple[float, float, float]) -> dict[str, Any]:
    lib = C.library_of(model)
    iv = {"value": b[0], "lo": b[1], "hi": b[2]}
    return {
        "library": lib, "library_name": C.LIBRARY_NAMES[lib], "label": C.label(model), "model_id": model,
        "is_setting": lib in C.LIBRARIES and model != L3, "mode": "faithful", "shadow_population": "standard",
        "tier": tier, "tier_name": C.TIER_WORDS[tier], "horizon": horizon,
        "horizon_seconds": C.parse_horizon(horizon) / 1e9, "fill_rate_bias_pp": iv,
        "exact_fill_rate_pct": {"value": 12.0, "lo": 11.5, "hi": 12.5}, "model_fill_rate_pct": 12.0 + b[0],
        "verdict": C.verdict_key(iv), "n": 5000, "n_symbols": 40, "n_dates": 3, "pnl_bias_ticks": None,
        "flagged_share": 0.0,
    }  # fmt: skip


def _write_api(root: Path) -> Path:
    """A site directory with `atlas/v1/` as `atlas_site.py` writes it."""
    base = root / "atlas" / "v1"
    (base / "symbols").mkdir(parents=True)
    rows = []
    for tier, models in BIAS.items():
        for m, b in models.items():
            rows.append(_summary_row(tier, m, "1min", b))
            rows.append(_summary_row(tier, m, "1s", (b[0] / 4, b[1] / 4, b[2] / 4)))
    summary = {"schema": "qt.atlas_summary", "schema_version": 1, "venue": "iex", "atlas_root": "ab" * 32,
               "headline_horizon": "1min", "rows": rows}  # fmt: skip

    def cfg(tier: str, model: str, config: str, kind: str) -> dict[str, Any]:
        b = BIAS[tier][model]
        iv = {"value": b[0], "lo": b[1], "hi": b[2]}
        v = C.verdict_key(iv)
        return {
            "library": C.library_of(model), "library_name": C.LIBRARY_NAMES[C.library_of(model)], "tier": tier,
            "tier_name": C.TIER_WORDS[tier], "label": C.label(model), "model_id": model, "n_settings": 2,
            "fill_rate_bias_pp": iv, "verdict": v, "verdict_text": C.VERDICT_TEXT[v], "overlapping": [],
            "overlapping_labels": [], "pnl_bias_ticks": None, "config": config, "config_kind": kind,
        }  # fmt: skip

    nt = "fill_model=FillModel(prob_fill_on_limit=1), book_type=BookType.L2_MBP, queue_position=True"
    hbt = ".power_prob_queue_model2(2).partial_fill_exchange()"
    configs = {
        "schema": "qt.atlas_configs", "schema_version": 1, "venue": "iex", "atlas_root": "ab" * 32,
        "mode": "faithful", "shadow_population": "standard", "horizon": "1min",
        "notes": {"nautilus": "FillModel is DefaultFillModel in 2.x."},
        "configs": [cfg(t, m, c, k) for t in BIAS for m, c, k in
                    ((PB2, hbt, "hftbacktest BacktestAsset chain"),
                     (QPOS, nt, "NautilusTrader BacktestEngine.add_venue keywords"))],
    }  # fmt: skip
    shard = {
        "schema": "qt.atlas_symbols", "schema_version": 1, "venue": "iex", "atlas_root": "ab" * 32, "letter": "Z",
        "mode": "faithful", "shadow_population": "standard", "horizons": ["1s", "10s", "1min"],
        "columns": ["bias_pp", "lo_pp", "hi_pp", "shrunk_pp", "n", "n_days"],
        "settings": [{"library": C.library_of(m), "label": C.label(m), "model_id": m} for m in (PB2, TOUCH, QPOS)],
        "symbols": [
            {"symbol": "ZZZA", "tier": "T1_top100", "days": 3, "cells": [
                [[-0.1, -0.3, 0.1, -0.1, 900, 3], [-0.3, -0.6, 0.0, -0.2, 900, 3], [-0.6, -1.0, -0.2, -0.5, 900, 3]],
                [[0.5, 0.2, 0.8, 0.4, 900, 3], None, [2.5, 1.5, 3.5, 2.1, 900, 3]],
                None,
            ]},
            {"symbol": "ZZZB", "tier": "T5_rest", "days": 1, "cells": [
                [None, None, [-0.4, None, None, -0.2, 30, 1]], None, None]},
        ],
    }  # fmt: skip
    files = {"summary.json": summary, "configs.json": configs, "symbols/Z.json": shard}
    entries = []
    for path, doc in files.items():
        data = (json.dumps(doc) + "\n").encode()
        (base / path).write_bytes(data)
        entries.append({"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    manifest = {
        "schema": "qt.atlas_api_manifest", "schema_version": 1, "api_version": 1, "built_at": "2026-11-10",
        "atlas_root": "ab" * 32, "venue": "iex", "venue_name": "IEX DEEP+",
        "window": {"from": "2026-11-06", "to": "2026-11-10"}, "days": ["2026-11-06", "2026-11-09", "2026-11-10"],
        "symbol_days": 120, "symbols": 41, "mode": "faithful", "shadow_population": "standard",
        "headline_horizon": "1min", "attribution": "Data provided for free by IEX.", "verify": "https://example.org",
        "files": entries,
    }  # fmt: skip
    (base / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    return root


@pytest.fixture
def site(tmp_path: Path) -> Path:
    return _write_api(tmp_path / "site")


def test_lookup_answers_a_library_first_in_plain_words(site: Path) -> None:
    res = qt.atlas.lookup(library="nautilus", source=site)
    lines = res.summary.splitlines()
    assert lines[0] == (
        "Within 1 minute, the real queue on IEX DEEP+ fills 12.00% of small resting orders (all symbols)."
    )
    assert lines[1] == (
        "NautilusTrader's default (touch fill) fills 1.50 pp more orders than the real queue (12% more fills; 95% "
        "interval +1.20 to +1.80 pp). Closest: NautilusTrader queue position (queue_position=True), +0.06 pp, no clear "
        "difference from the real queue."
    )
    assert [r["model_id"] for r in res.rows] == [TOUCH, QPOS]  # its settings only, no bound
    assert res.rows[0]["verdict"] == "too_optimistic" and res.rows[1]["verdict"] == "no_clear_difference"
    (c,) = res.configs
    assert c["model_id"] == QPOS and c["config"].endswith("queue_position=True") and "DefaultFillModel" in c["note"]
    text = res.text()
    assert text.startswith(lines[0]) and "config to paste (NautilusTrader BacktestEngine.add_venue keywords):" in text
    assert "  NautilusTrader default (touch fill)" in text and "+1.50 [+1.20, +1.80] n=5,000" in text
    assert "Data provided for free by IEX." in text and "atlas root abababababababab" in text
    df = res.table()
    assert list(df["setting"]) == ["NautilusTrader default (touch fill)", "NautilusTrader queue position (queue_"
                                   "position=True)"] and df["bias_pp"].iloc[0] == 1.5  # fmt: skip
    # every library, another tier and horizon; hftbacktest's order-level model is in its table, a bound is not
    res = qt.atlas.lookup(tier="T1_top100", horizon="1s", source=site / "atlas" / "v1")
    assert {r["library"] for r in res.rows} == {"nautilus", "hftbacktest"} and L3 in {r["model_id"] for r in res.rows}
    assert FRONT not in {r["model_id"] for r in res.rows} and res.doc["tier_name"] == "the 100 most traded symbols"
    assert any("closest settings within 1min" in w for w in res.doc["warnings"])  # configs are at the site horizon


def test_lookup_by_symbol_reads_its_shard(site: Path) -> None:
    res = qt.atlas.lookup(library="nautilus", symbol="zzza", source=site)
    assert res.doc["symbol"] == "ZZZA" and res.doc["tier"] == "T1_top100"
    assert res.summary.splitlines() == [
        "ZZZA on IEX DEEP+ (the 100 most traded symbols, tier T1_top100; 3 days), within 1 minute:",
        "NautilusTrader's default (touch fill) fills 2.50 pp more orders than the real queue (95% interval +1.50 to "
        "+3.50 pp). It is also the closest of its settings in the atlas.",
    ]
    # 1s and 1min (the 10s cell is missing), with n and days; the tier's config, said to be the tier's
    assert [(r["horizon"], r["n"], r["days"]) for r in res.rows] == [("1s", 900, 3), ("1min", 900, 3)]
    assert res.configs[0]["model_id"] == QPOS and any("ZZZA's tier" in w for w in res.doc["warnings"])
    one = qt.atlas.lookup(symbol="ZZZB", source=site)
    assert one.rows[0]["fill_rate_bias_pp"] == {"value": -0.4, "lo": None, "hi": None}
    assert "[no interval]" in one.text() and any("one day" in w for w in one.doc["warnings"])
    assert one.doc["configs"] == []  # no config for tier T5_rest in this atlas
    with pytest.raises(QtError, match="not in the atlas") as e:
        qt.atlas.lookup(symbol="AAPL", source=site)
    assert e.value.code == "QT-USAGE-001"
    for kw, match in (({"library": "zipline"}, "unknown library"), ({"tier": "T9"}, "no tier"),
                      ({"horizon": "5min"}, "no horizon"), ({"horizon": "soon"}, "not a horizon")):  # fmt: skip
        with pytest.raises(QtError, match=match):
            qt.atlas.lookup(source=site, **kw)


def test_a_damaged_or_foreign_file_is_refused(site: Path, tmp_path: Path) -> None:
    p = site / "atlas" / "v1" / "configs.json"
    p.write_text(p.read_text().replace("prob_fill_on_limit=1", "prob_fill_on_limit=0"))
    with pytest.raises(QtError, match="does not match the sha256") as e:
        qt.atlas.lookup(source=site)
    assert e.value.code == "QT-INPUT-002"
    with pytest.raises(QtError, match=r"no manifest\.json") as e:
        qt.atlas.lookup(source=tmp_path / "nowhere")
    assert e.value.code == "QT-INPUT-001"
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "manifest.json").write_text(json.dumps({"schema": "qt.error"}))
    with pytest.raises(QtError, match="not an atlas API document"):
        qt.atlas.lookup(source=bad)


def test_remote_copy_needs_consent_checks_hashes_and_is_cached(site: Path, tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("QT_CACHE_DIR", str(tmp_path / "cache"))
    base = site / "atlas" / "v1"
    url = "https://atlas.example/queuetruth/atlas/v1/"
    got: list[str] = []

    def fetch(u: str) -> bytes:
        got.append(u)
        return (base / u.removeprefix(url)).read_bytes()

    # nothing is downloaded without consent
    with pytest.raises(QtError, match="nothing was downloaded") as e:
        qt.atlas.lookup(source=url, fetch=fetch)
    assert got == [] and "--yes" in (e.value.hint or "")
    with pytest.raises(QtError, match="nothing was downloaded"):
        qt.atlas.lookup(source=url, fetch=fetch, confirm=lambda what: what == "no")
    assert got == []
    # with consent: the manifest and the two files the answer needs, then offline from the cache
    res = qt.atlas.lookup(library="hftbacktest", source=url, fetch=fetch, yes=True)
    assert got == [f"{url}manifest.json", f"{url}summary.json", f"{url}configs.json"]
    cache = tmp_path / "cache" / "atlas" / "atlas.example" / "queuetruth" / "atlas" / "v1"
    assert (cache / "summary.json").read_bytes() == (base / "summary.json").read_bytes()
    assert res.doc["source"] == f"{url} (cached in {cache})"
    got.clear()
    again = qt.atlas.lookup(library="hftbacktest", source=url, fetch=fetch)  # no consent needed: nothing to fetch
    assert got == [] and again.doc == res.doc
    # a symbol needs its shard: one more file, after consent
    with pytest.raises(QtError, match="nothing was downloaded"):
        qt.atlas.lookup(symbol="ZZZA", source=url, fetch=fetch)
    qt.atlas.lookup(symbol="ZZZA", source=url, fetch=fetch, confirm=lambda what: True)
    assert got == [f"{url}symbols/Z.json"]
    # a file that does not match the manifest is never cached or used
    got.clear()
    (cache / "configs.json").unlink()
    with pytest.raises(QtError, match="does not match the sha256"):
        qt.atlas.lookup(source=url, fetch=lambda u: b'{"schema": "qt.atlas_configs"}', yes=True)
    assert not (cache / "configs.json").exists()
    # --refresh downloads the manifest again; plain http is refused
    qt.atlas.lookup(source=url, fetch=fetch, yes=True, refresh=True)
    assert got[0] == f"{url}manifest.json"
    with pytest.raises(QtError, match="https"):
        L.open_source("http://atlas.example/v1/", yes=True)
    assert L.DEFAULT_URL == "https://sarptandoven.github.io/queuetruth/atlas/v1/"


@pytest.mark.requires_core
def test_cli_prints_the_answer_table_and_config(site: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["atlas", "--library", "hftbacktest", "--from", str(site)]) == 0
    out = capsys.readouterr()
    lines = out.out.splitlines()
    assert lines[0].startswith("Within 1 minute, the real queue on IEX DEEP+ fills 12.00%")
    assert lines[1] == (
        "hftbacktest Power2 queue model (n=2) fills 0.80 pp fewer orders than the real queue (7% fewer fills; 95% "
        "interval -1.00 to -0.60 pp)."
    )
    assert "    .power_prob_queue_model2(2).partial_fill_exchange()" in lines
    assert out.err == ""
    assert cli.main(["--format", "json", "atlas", "--symbol", "ZZZA", "--from", str(site)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "qt.atlas_answer" and doc["symbol"] == "ZZZA" and len(doc["configs"]) == 2
    assert cli.main(["atlas", "--json", "--library", "nautilus", "--from", str(site)]) == 0
    assert json.loads(capsys.readouterr().out)["library"] == "nautilus"
    # errors are qt.error documents with exit code 2; the public site is never fetched without --yes
    assert cli.main(["atlas", "--symbol", "NOPE", "--from", str(site), "--json"]) == 2
    err = json.loads(capsys.readouterr().out)
    assert err["code"] == "QT-USAGE-001" and "NOPE" in err["message"]
    assert cli.main(["atlas", "--library", "zipline"]) == 2
    assert "invalid choice" in capsys.readouterr().err
    assert cli.main(["help", "atlas"]) == 0
    assert "--from DIR_OR_URL" in capsys.readouterr().out


def test_cli_asks_before_downloading(monkeypatch: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from queuetruth.atlas import _cli

    monkeypatch.setenv("QT_CACHE_DIR", str(tmp_path / "cache"))
    calls: list[str] = []
    monkeypatch.setattr(L, "_get", lambda u, timeout_s=30.0: calls.append(u) or b"")
    # not a terminal: refused with the way forward, and nothing fetched
    assert _cli.main(["atlas"]) == 2
    err = capsys.readouterr().err
    assert "needs to download the atlas data API" in err and "--yes" in err and calls == []
    # on a terminal: the prompt, and "no" means no
    monkeypatch.setattr(_cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(_cli.sys.stderr, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(_cli, "_ask", lambda prompt: "n\n")
    assert _cli.main(["atlas"]) == 2 and calls == []
    assert "nothing was downloaded" in capsys.readouterr().err
