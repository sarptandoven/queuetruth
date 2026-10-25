"""Helpers that read the answer-first report's structure (shared by the report tests)."""

from __future__ import annotations

import re


def section(html: str, sid: str) -> str:
    """Inner HTML of `<section id="sid" ...>` (sections are never nested)."""
    m = re.search(rf'<section id="{sid}"[^>]*>(.*?)</section>', html, re.S)
    assert m, f"section {sid} missing"
    return m.group(1)


def first_table_rows(fragment: str) -> list[str]:
    """Row-header texts of the first table in `fragment`, in order (`<wbr>` break hints removed)."""
    body = fragment.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    return [re.sub(r"<wbr>", "", t) for t in re.findall(r'<tr(?: class="[^"]*")?><th scope="row">(.*?)</th>', body)]


def leaderboard_rows(html: str) -> list[str]:
    """Rows of the compact leaderboard: the exact fill-rate row, then the models in rank order."""
    return first_table_rows(section(html, "leaderboard"))
