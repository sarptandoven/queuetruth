"""The docs checks added for the "What is validated" page and stale version wording (docs/check_docs.py)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "docs" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_stale_version_wording_is_flagged(tmp_path: Path) -> None:
    cd = _load("check_docs")
    page = tmp_path / "page.md"
    page.write_text("ok line\nNew in v0.1 only.\nShips in the next release.\nSince 0.3.0 it works.\n", encoding="utf-8")
    res = cd.Result()
    cd.check_stale_versions(page, res)
    assert len(res.failures) == 2
    assert ":2:" in res.failures[0] and ":3:" in res.failures[1]


def test_stale_version_wording_exempts_design_and_launch() -> None:
    cd = _load("check_docs")
    res = cd.Result()
    for p in sorted((ROOT / "docs" / "design").rglob("*.md")) + sorted((ROOT / "docs" / "launch").rglob("*.md")):
        cd.check_stale_versions(p, res)
    assert res.failures == []


def test_not_in_nav_patterns_are_read(tmp_path: Path) -> None:
    cd = _load("check_docs")
    cfg = tmp_path / "mkdocs.yml"
    cfg.write_text(
        "site_name: x\nnot_in_nav: |\n  design/extensions/*.md\n  /concepts/a.md\nnav:\n  - Home: index.md\n"
    )
    assert cd.not_in_nav(cfg) == ["design/extensions/*.md", "concepts/a.md"]
    assert cd.nav_pages(cfg) == ["index.md"]


def test_nav_skips_external_links(tmp_path: Path) -> None:
    cd = _load("check_docs")
    cfg = tmp_path / "mkdocs.yml"
    cfg.write_text("nav:\n  - Home: index.md\n  - Roadmap: https://example.org/ROADMAP.md\n")
    assert cd.nav_pages(cfg) == ["index.md"]


def test_validated_outputs_are_current() -> None:
    p = subprocess.run(
        [sys.executable, str(ROOT / "docs" / "gen_validated.py"), "--check"], capture_output=True, text=True
    )
    assert p.returncode == 0, p.stdout + p.stderr


def test_validated_rows_have_known_status_and_evidence() -> None:
    gv = _load("gen_validated")
    rows = gv.load()
    assert {r["status"] for r in rows} <= set(gv.STATUS)
    for r in rows:
        assert r["evidence"], r["area"]
        for _, link in r["evidence"]:
            assert (ROOT / "docs" / link.split("#")[0]).exists(), link


def test_site_links_must_name_a_page_and_heading(tmp_path: Path) -> None:
    cd = _load("check_docs")
    site = cd.DOCS_SITE
    page = tmp_path / "README.md"
    page.write_text(
        f"[home]({site}) [ok]({site}how-to/check-my-backtest/) [anchor]({site}RESULTS/#the-headline-number)\n"
        f"[gone]({site}how-to/no-such-page/)\n"
        f"[bad anchor]({site}RESULTS/#no-such-heading)\n"
        f"[draft]({site}launch/social/)\n",
        encoding="utf-8",
    )
    res = cd.Result()
    cd.check_site_links(page, res)
    assert len(res.failures) == 3
    assert "no-such-page" in res.failures[0] and ":2:" in res.failures[0]
    assert "#no-such-heading" in res.failures[1]
    assert "launch/social" in res.failures[2]


def test_readme_and_launch_site_links_resolve() -> None:
    cd = _load("check_docs")
    res = cd.Result()
    for p in [ROOT / "README.md", *sorted((ROOT / "docs" / "launch").glob("*.md"))]:
        cd.check_site_links(p, res)
    assert res.failures == []
