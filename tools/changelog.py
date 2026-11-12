"""Changelog fragments: one file per change in `changelog.d/`, folded into CHANGELOG.md at release (stdlib only).

Every pull request that users will notice adds one file instead of editing CHANGELOG.md, so parallel pull requests
never conflict on it:

    changelog.d/<slug>.<section>.md

`<slug>` is lowercase letters, digits and hyphens (the branch or topic, for example `compare-watch`); `<section>` is
one of the Keep a Changelog sections below. The file holds one or more Markdown list items ("- ..."), continuation
lines indented by two spaces, written as CHANGELOG.md entries are (plain sentences, no em or en dashes).

    python tools/changelog.py check            # validate the fragments (make lint runs it)
    python tools/changelog.py show             # the Unreleased section as it will read, fragments included
    python tools/changelog.py release X.Y.Z [--date YYYY-MM-DD]
                                               # fold the fragments and the Unreleased section into
                                               # `## [X.Y.Z] - DATE`, start an empty Unreleased, delete the fragments

Within a section, the entries already under `## [Unreleased]` come first, then the fragments in file-name order.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHANGELOG = REPO / "CHANGELOG.md"
FRAGMENTS = REPO / "changelog.d"
#: Keep a Changelog 1.1 sections, in the order they are written.
SECTIONS = ("added", "changed", "deprecated", "removed", "fixed", "security")
NAME = re.compile(r"^[a-z0-9][a-z0-9-]*\.(" + "|".join(SECTIONS) + r")\.md$")
DASHES = ("—", "–")
UNRELEASED = "## [Unreleased]"


def fragment_files(root: Path = FRAGMENTS) -> list[Path]:
    """The fragment files (every file of `root` but README.md), sorted by name."""
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_file() and p.name != "README.md")


def check_fragment(path: Path) -> list[str]:
    """What is wrong with one fragment file (empty when it is fine)."""
    errors: list[str] = []
    name = path.name
    if not NAME.match(name):
        errors.append(
            f"{name}: name must be <slug>.<section>.md, slug in [a-z0-9-], section one of {', '.join(SECTIONS)}"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [*errors, f"{name}: not UTF-8 text"]
    if not text.strip():
        return [*errors, f"{name}: empty"]
    if not text.endswith("\n"):
        errors.append(f"{name}: must end with a newline")
    lines = text.rstrip("\n").split("\n")
    if not lines[0].startswith("- "):
        errors.append(f"{name}:1: must start with a list item ('- ')")
    for i, line in enumerate(lines, 1):
        if not line:
            errors.append(f"{name}:{i}: blank line inside a fragment (one list item per change, no blank lines)")
        elif not (line.startswith("- ") or line.startswith("  ")):
            errors.append(f"{name}:{i}: a line must be a list item ('- ') or a continuation indented by two spaces")
        if any(d in line for d in DASHES):
            errors.append(f"{name}:{i}: em or en dash (write plain sentences, as the rest of CHANGELOG.md)")
        if line.startswith("#"):
            errors.append(f"{name}:{i}: no headings; the section comes from the file name")
    return errors


def check(root: Path = FRAGMENTS) -> list[str]:
    errors: list[str] = []
    if root.is_dir():
        for p in sorted(root.iterdir()):
            if p.is_dir():
                errors.append(f"{p.name}: a directory inside changelog.d/")
    for p in fragment_files(root):
        errors.extend(check_fragment(p))
    return errors


def _split_unreleased(text: str) -> tuple[str, str, str]:
    """(before the Unreleased heading, the Unreleased body, from the next version heading on)."""
    start = text.find(UNRELEASED + "\n")
    if start < 0:
        raise SystemExit(f"CHANGELOG.md: no '{UNRELEASED}' heading")
    body_start = start + len(UNRELEASED) + 1
    m = re.search(r"^## \[", text[body_start:], re.M)
    end = body_start + m.start() if m else len(text)
    return text[:start], text[body_start:end], text[end:]


def _sections(body: str) -> tuple[str, dict[str, list[str]]]:
    """The text before the first `### ` heading, and each section's lines (Keep a Changelog names, lowercased)."""
    parts = re.split(r"^### (.+)$", body, flags=re.M)
    head, out = parts[0], {}
    for title, content in zip(parts[1::2], parts[2::2], strict=True):
        out.setdefault(title.strip().lower(), []).extend(content.strip("\n").split("\n") if content.strip() else [])
    return head, out


def merged_unreleased(text: str, files: list[Path]) -> str:
    """The Unreleased body with the fragments added, sections in Keep a Changelog order (unknown ones after)."""
    _, body, _ = _split_unreleased(text)
    head, sections = _sections(body)
    for p in files:
        sec = p.name.split(".")[-2]
        lines = p.read_text(encoding="utf-8").rstrip("\n").split("\n")
        sections.setdefault(sec, [])
        if sections[sec]:
            sections[sec].append("")
        sections[sec].extend(lines)
    order = [s for s in SECTIONS if s in sections] + [s for s in sections if s not in SECTIONS]
    out = head.strip("\n")
    chunks = [out] if out else []
    for s in order:
        if not sections[s]:
            continue
        chunks.append(f"### {s.capitalize()}\n\n" + "\n".join(sections[s]).strip("\n"))
    return "\n\n".join(chunks)


def show(changelog: Path = CHANGELOG, root: Path = FRAGMENTS) -> str:
    body = merged_unreleased(changelog.read_text(encoding="utf-8"), fragment_files(root))
    return f"{UNRELEASED}\n\n{body}\n" if body else f"{UNRELEASED}\n"


def release(version: str, date: str, changelog: Path = CHANGELOG, root: Path = FRAGMENTS) -> list[Path]:
    """Fold the fragments and the Unreleased section into `## [version] - date`; returns the deleted fragments."""
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?", version):
        raise SystemExit(f"not a version: {version!r}")
    dt.date.fromisoformat(date)
    text = changelog.read_text(encoding="utf-8")
    if re.search(rf"^## \[{re.escape(version)}\]", text, re.M):
        raise SystemExit(f"CHANGELOG.md already has a [{version}] section")
    errors = check(root)
    if errors:
        raise SystemExit("\n".join(errors))
    files = fragment_files(root)
    before, _, after = _split_unreleased(text)
    body = merged_unreleased(text, files)
    changelog.write_text(f"{before}{UNRELEASED}\n\n## [{version}] - {date}\n\n{body}\n\n{after}", encoding="utf-8")
    for p in files:
        p.unlink()
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="validate changelog.d/ fragments")
    sub.add_parser("show", help="print the Unreleased section with the fragments folded in")
    r = sub.add_parser("release", help="fold everything into a version section and delete the fragments")
    r.add_argument("version")
    r.add_argument("--date", default=dt.date.today().isoformat())
    a = ap.parse_args(argv)
    if a.cmd == "check":
        errors = check()
        for e in errors:
            print(f"changelog.d/{e}", file=sys.stderr)
        n = len(fragment_files())
        print(f"changelog: {'ok' if not errors else f'{len(errors)} error(s)'} ({n} fragment(s))")
        return 1 if errors else 0
    if a.cmd == "show":
        sys.stdout.write(show())
        return 0
    gone = release(a.version, a.date)
    print(f"CHANGELOG.md: [{a.version}] - {a.date} written, {len(gone)} fragment(s) folded in and deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
