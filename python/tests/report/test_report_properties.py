"""Whole-page properties of the report (SPEC 5.9, 7.2): byte determinism, no external references, well-formed HTML
and SVG, complete without JavaScript, size bound on a 5,000-order scorecard, provenance paths as given, errors."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import jsonschema
import pytest

import queuetruth.report as report_mod
from queuetruth.errors import QtError
from queuetruth.report import render
from report import synth as rc

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
SVG_SELF_CLOSING = {"line", "path", "circle", "rect", "polyline", "polygon", "stop", "use"}


class TagChecker(HTMLParser):
    """Strict nesting check: every non-void element is closed in order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for k, v in attrs:
            if k == "id" and v:
                self.ids.append(v)
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for k, v in attrs:
            if k == "id" and v:
                self.ids.append(v)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}> with open {self.stack[-3:]}")
            return
        self.stack.pop()


def _render(tmp_path: Path, inputs: list[Any], name: str = "report.html", **kw: Any) -> tuple[Any, bytes]:
    out = tmp_path / name
    summary = render({"inputs": [str(i) for i in inputs], "out": str(out), **kw})
    return summary, out.read_bytes()


def test_byte_determinism_render_twice(bundle: Any, tmp_path: Path) -> None:
    s1, b1 = _render(tmp_path / "a", bundle.inputs, max_why=6)
    s2, b2 = _render(tmp_path / "b", bundle.inputs, max_why=6)
    assert b1 == b2
    assert s1.sha256 == s2.sha256 == hashlib.sha256(b1).hexdigest()
    assert s1.bytes == len(b1)
    assert s1.model_dump(exclude={"out"}) == s2.model_dump(exclude={"out"})


def test_no_external_urls(rendered: tuple[Any, str]) -> None:
    _, html = rendered
    assert not re.search(r"""\b(?:src|href|xlink:href|action|poster)\s*=\s*["']?\s*(?:https?:)?//""", html, re.I)
    assert not re.search(r"""url\(\s*["']?\s*(?:https?:)?//""", html, re.I)
    assert not re.search(r"@import", html)
    assert "<link" not in html and "<iframe" not in html and "<img" not in html


def test_html_parses_and_every_svg_is_xml(rendered: tuple[Any, str]) -> None:
    _, html = rendered
    chk = TagChecker()
    chk.feed(html)
    chk.close()
    assert chk.errors == []
    assert chk.stack == []
    assert len(chk.ids) == len(set(chk.ids)), "duplicate element ids"
    svgs = re.findall(r"<svg\b.*?</svg>", html, re.S)
    assert len(svgs) > 40
    for s in svgs:
        root = ET.fromstring(s)
        assert root.tag == "{http://www.w3.org/2000/svg}svg"
        assert root[0].tag == "{http://www.w3.org/2000/svg}title" and (root[0].text or "").strip()
        assert root[1].tag == "{http://www.w3.org/2000/svg}desc"


def test_every_chart_figure_has_a_data_table(rendered: tuple[Any, str]) -> None:
    _, html = rendered
    figs = re.findall(r'<figure class="fig">(.*?)</figure>', html, re.S)
    assert figs
    for f in figs:
        assert "<svg" in f and "<details><summary>Data table</summary>" in f
    rel = re.findall(r'<div class="relgrid">.*?</div>(<details>|<h4>|<div|</section>)', html, re.S)
    assert rel and all(r == "<details>" for r in rel)


def test_renders_without_javascript(rendered: tuple[Any, str]) -> None:
    summary, html = rendered
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, re.S)
    assert len(scripts) <= 1
    for attrs, body in scripts:
        assert "src" not in attrs
        assert "clipboard" in body  # copy buttons only
    assert not re.search(r"\son[a-z]+\s*=", html)
    stripped = re.sub(r"<script\b.*?</script>", "", html, flags=re.S)
    for sid in summary.sections[1:]:
        assert f'<section id="{sid}" ' in stripped
    assert "<noscript" not in html


def test_light_and_dark_themes(rendered: tuple[Any, str]) -> None:
    _, html = rendered
    assert "prefers-color-scheme: dark" in html
    assert '<meta name="color-scheme" content="light dark">' in html
    assert re.search(r"body\{[^}]*background:var\(--bg\)", html)


def test_size_bound_on_5000_order_scorecard(tmp_path: Path) -> None:
    rc.make_score_dir(tmp_path / "score", n=5000, seed=9)
    summary, data = _render(tmp_path, [tmp_path / "score"])
    assert summary.bytes == len(data) <= 10 * 1024 * 1024
    assert "verdict" in summary.sections and "position" in summary.sections


def test_compact_fallback_keeps_sections(bundle: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    full, b_full = _render(tmp_path / "a", bundle.inputs, max_why=6)
    monkeypatch.setattr(report_mod, "MAX_BYTES", 1000)
    small, b_small = _render(tmp_path / "b", bundle.inputs, max_why=6)
    assert small.sections == full.sections
    assert len(b_small) < len(b_full)


def test_no_absolute_paths_when_inputs_are_relative(
    bundle: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(bundle.root)
    out = tmp_path / "r.html"
    summary = render({"inputs": ["score", "zoo", "cal", "hazard", "replay"], "out": str(out), "max_why": 4})
    html = out.read_text(encoding="utf-8")
    assert str(bundle.root) not in html
    assert str(Path.home()) not in html
    assert summary.inputs == ["score", "zoo", "cal", "hazard", "replay"]


def test_summary_matches_json_schema(rendered: tuple[Any, str], repo_root: Path) -> None:
    summary, _ = rendered
    schema = json.loads((repo_root / "schemas" / "json" / "qt.report_summary.schema.json").read_text())
    jsonschema.validate(json.loads(summary.model_dump_json()), schema)
    assert summary.schema_ == "qt.report_summary"


def test_errors(tmp_path: Path) -> None:
    with pytest.raises(QtError) as e:
        render({"inputs": [str(tmp_path / "missing")], "out": str(tmp_path / "r.html")})
    assert e.value.code == "QT-INPUT-001"
    (tmp_path / "empty").mkdir()
    with pytest.raises(QtError) as e:
        render({"inputs": [str(tmp_path / "empty")], "out": str(tmp_path / "r.html")})
    assert e.value.code == "QT-INPUT-002"
    with pytest.raises(QtError) as e:
        render({"inputs": [str(tmp_path / "empty")]})
    assert e.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError) as e:
        render({"inputs": [], "out": "x.html"})
    assert e.value.code == "QT-CONFIG-001"
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "summary.json").write_text(json.dumps({"schema": "qt.zoo_summary", "schema_version": 1}), encoding="utf-8")
    with pytest.raises(QtError) as e:
        render({"inputs": [str(bad)], "out": str(tmp_path / "r.html")})
    assert e.value.code == "QT-INPUT-002"


def test_single_file_input(bundle: Any, tmp_path: Path) -> None:
    summary, data = _render(tmp_path, [bundle.score / "summary.json"])
    assert summary.sections[:3] == ["header", "answer", "verdict"]
    assert b"qt.score_orders" not in data.split(b"<summary><h2>Inputs</h2>")[1].split(b"</table>")[0]
