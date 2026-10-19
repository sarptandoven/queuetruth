"""L3 (security review, inverted): MCP local tools resolve every path, including the children found inside a
directory argument, with realpath and refuse anything outside `QT_MCP_ROOT` (symlinked children included)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from queuetruth.errors import QtError
from queuetruth.mcp.local import LocalOutputs

SECRET = {
    "schema": "qt.error",
    "schema_version": 1,
    "code": "QT-USAGE-001",
    "message": "OUTSIDE-ROOT",
    "details": {"token": "s3cr3t"},
}


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    root, outside = tmp_path / "root", tmp_path / "outside"
    (root / "run").mkdir(parents=True)
    outside.mkdir()
    (outside / "doc.json").write_text(json.dumps(SECRET))
    return root, outside


def test_symlinked_summary_children_are_refused(tree):
    root, outside = tree
    lo = LocalOutputs(root)
    with pytest.raises(QtError) as e:
        lo.read_summary(str(outside / "doc.json"))  # the direct path
    assert e.value.code == "QT-CONFIG-001"
    (root / "run" / "summary.json").symlink_to(outside / "doc.json")
    with pytest.raises(QtError) as e:
        lo.read_summary("run")  # preferred-name child
    assert e.value.code == "QT-CONFIG-001" and "s3cr3t" not in str(e.value.message)
    (root / "run2").mkdir()
    (root / "run2" / "anything.json").symlink_to(outside / "doc.json")
    with pytest.raises(QtError) as e:
        lo.read_summary("run2")  # glob child: skipped, so nothing qualifies
    assert "OUTSIDE-ROOT" not in str(e.value.message)
    with pytest.raises(QtError) as e:
        lo.load_document(root / "run2" / "anything.json")
    assert e.value.code == "QT-CONFIG-001"


def test_listing_skips_children_that_leave_the_root(tree):
    root, outside = tree
    (root / "run" / "summary.json").symlink_to(outside / "doc.json")
    (outside / "sub").mkdir()
    (outside / "sub" / "doc.json").write_text(json.dumps(SECRET))
    (root / "linked_dir").symlink_to(outside / "sub", target_is_directory=True)
    inner = root / "inner.json"
    inner.write_text(json.dumps(SECRET))
    (root / "run" / "alias.json").symlink_to(inner)  # inside the root: still listed
    entries = LocalOutputs(root).list_outputs(".")["entries"]
    assert entries == [{"path": "inner.json", "schema": "qt.error"}, {"path": "run/alias.json", "schema": "qt.error"}]


def test_why_refuses_a_dataset_with_symlinks_out_of_the_root(tree):
    root, outside = tree
    ds = root / "dataset" / "mbo"
    ds.mkdir(parents=True)
    (root / "shadows.parquet").write_bytes(b"PAR1")
    (ds / "part.parquet").symlink_to(outside / "doc.json")
    calls: list[str] = []
    with pytest.raises(QtError) as e:
        LocalOutputs(root).why("dataset", "shadows.parquet", "s1", calls.append)  # type: ignore[arg-type]
    assert e.value.code == "QT-CONFIG-001" and calls == []
