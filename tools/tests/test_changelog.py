"""tools/changelog.py: fragment checks, the Unreleased preview and the release fold, on temporary copies only."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import changelog as cl

HEAD = """# Changelog

Intro.

## [Unreleased]

### Added

- Existing added entry.

### Fixed

- Existing fix,
  over two lines.

## [0.3.0] - 2026-11-07

### Added

- Old entry.
"""


def _setup(tmp_path: Path, frags: dict[str, str]) -> tuple[Path, Path]:
    log = tmp_path / "CHANGELOG.md"
    log.write_text(HEAD, encoding="utf-8")
    root = tmp_path / "changelog.d"
    root.mkdir()
    (root / "README.md").write_text("# not a fragment\n", encoding="utf-8")
    for name, text in frags.items():
        (root / name).write_text(text, encoding="utf-8")
    return log, root


def test_the_repository_fragments_are_valid() -> None:
    assert cl.check() == []


@pytest.mark.parametrize(
    ("name", "text", "want"),
    [
        ("Bad_Name.added.md", "- x\n", "name must be"),
        ("topic.notes.md", "- x\n", "name must be"),
        ("topic.added.md", "", "empty"),
        ("topic.added.md", "- x", "newline"),
        ("topic.added.md", "x\n", "must start with a list item"),
        ("topic.added.md", "- x\n\n- y\n", "blank line"),
        ("topic.added.md", "- x\nloose line\n", "continuation"),
        ("topic.added.md", "- a — b\n", "dash"),
        ("topic.added.md", "- a – b\n", "dash"),
    ],
)
def test_bad_fragments_are_named(tmp_path: Path, name: str, text: str, want: str) -> None:
    _, root = _setup(tmp_path, {name: text})
    errors = cl.check(root)
    assert errors and any(want in e for e in errors), errors


def test_show_merges_fragments_by_section_in_order(tmp_path: Path) -> None:
    log, root = _setup(
        tmp_path,
        {
            "b-topic.added.md": "- B added.\n",
            "a-topic.added.md": "- A added,\n  continued.\n",
            "c-topic.security.md": "- C security.\n",
            "d-topic.changed.md": "- D changed.\n",
        },
    )
    assert cl.check(root) == []
    out = cl.show(log, root)
    assert out == (
        "## [Unreleased]\n\n"
        "### Added\n\n- Existing added entry.\n\n- A added,\n  continued.\n\n- B added.\n\n"
        "### Changed\n\n- D changed.\n\n"
        "### Fixed\n\n- Existing fix,\n  over two lines.\n\n"
        "### Security\n\n- C security.\n"
    )
    assert log.read_text(encoding="utf-8") == HEAD  # show writes nothing


def test_release_folds_everything_and_deletes_the_fragments(tmp_path: Path) -> None:
    log, root = _setup(tmp_path, {"x.fixed.md": "- New fix.\n"})
    gone = cl.release("0.4.0", "2026-11-20", log, root)
    assert [p.name for p in gone] == ["x.fixed.md"]
    assert [p.name for p in root.iterdir()] == ["README.md"]
    text = log.read_text(encoding="utf-8")
    assert text.startswith("# Changelog\n\nIntro.\n\n## [Unreleased]\n\n## [0.4.0] - 2026-11-20\n\n### Added\n")
    assert "### Fixed\n\n- Existing fix,\n  over two lines.\n\n- New fix.\n\n## [0.3.0] - 2026-11-07\n" in text
    assert text.endswith("### Added\n\n- Old entry.\n")
    # the release workflow's check: one `## [X.Y.Z] - YYYY-MM-DD` heading for the version
    assert text.count("## [0.4.0] - 2026-11-20\n") == 1
    # the empty Unreleased section is ready for the next fragments
    assert cl.show(log, root) == "## [Unreleased]\n"


def test_release_refuses_a_bad_version_a_repeat_or_a_bad_fragment(tmp_path: Path) -> None:
    log, root = _setup(tmp_path, {"x.fixed.md": "- ok\n"})
    with pytest.raises(SystemExit):
        cl.release("v0.4", "2026-11-20", log, root)
    with pytest.raises(SystemExit):
        cl.release("0.3.0", "2026-11-20", log, root)
    (root / "bad.added.md").write_text("no bullet\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        cl.release("0.4.0", "2026-11-20", log, root)
    assert log.read_text(encoding="utf-8") == HEAD and (root / "x.fixed.md").exists()


def test_the_real_changelog_has_an_unreleased_section() -> None:
    out = cl.show()
    assert out.startswith("## [Unreleased]\n\n### ")
