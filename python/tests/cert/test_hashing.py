"""Hashing, normalized digests and JSON pointers of `queuetruth.cert.hashing`."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from queuetruth.cert.hashing import (
    canonical_value_text,
    escape_token,
    json_pointer,
    normalized_sha256,
    pointer_tokens,
    sha256_file,
    strip_volatile,
    tree_files,
)
from queuetruth.errors import QtError


def test_sha256_file_streams_large_files(tmp_path: Path) -> None:
    data = os.urandom(3 * (1 << 20) + 17)  # more than one chunk
    p = tmp_path / "big.bin"
    p.write_bytes(data)
    assert sha256_file(p) == hashlib.sha256(data).hexdigest()


def test_sha256_file_missing_is_input_error(tmp_path: Path) -> None:
    with pytest.raises(QtError) as e:
        sha256_file(tmp_path / "nope")
    assert e.value.code == "QT-INPUT-001"


def test_tree_files_sorted_by_posix_path_and_complete(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a-b").write_text("x")
    (tmp_path / "a" / "b" / "c").write_text("yy")
    (tmp_path / "z").write_text("")
    got = tree_files(tmp_path)
    assert [p for p, _, _ in got] == ["a-b", "a/b/c", "z"]  # '-' sorts before '/'
    assert got[1][1] == 2
    assert got[2][2] == hashlib.sha256(b"").hexdigest()


def test_tree_files_refuses_symlinks(tmp_path: Path) -> None:
    (tmp_path / "real").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(QtError) as e:
        tree_files(tmp_path)
    assert e.value.code == "QT-INPUT-002"
    assert "symbolic link" in e.value.message


def test_tree_files_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(QtError) as e:
        tree_files(tmp_path / "missing")
    assert e.value.code == "QT-INPUT-001"


def test_normalized_digest_ignores_timings_and_execution_only_spec_fields(tmp_path: Path) -> None:
    base = {"schema": "qt.x", "spec": {"dataset": {"path": "d"}, "out": "a", "threads": 1}, "v": 1.5}
    a = {**base, "timings": {"wall_s": 1.0}, "nested": {"timings": {"x": 2}, "k": [1, 2]}}
    b = {
        **base,
        "spec": {"dataset": {"path": "d"}, "out": "elsewhere", "threads": 4},
        "timings": {"wall_s": 99.0},
        "nested": {"timings": {"x": 3}, "k": [1, 2]},
    }
    pa, pb = tmp_path / "a.json", tmp_path / "b.json"
    pa.write_text(json.dumps(a, indent=2))
    pb.write_text(json.dumps(b, sort_keys=True))  # key order and whitespace do not matter
    assert sha256_file(pa) != sha256_file(pb)
    assert normalized_sha256(pa) == normalized_sha256(pb)
    c = {**b, "v": 1.25}
    pc = tmp_path / "c.json"
    pc.write_text(json.dumps(c))
    assert normalized_sha256(pc) != normalized_sha256(pa)


def test_normalized_digest_keeps_nested_out_fields(tmp_path: Path) -> None:
    """Only the top-level spec echo loses `out`; an `out` anywhere else is data."""
    pa, pb = tmp_path / "a.json", tmp_path / "b.json"
    pa.write_text(json.dumps({"x": {"out": 1}}))
    pb.write_text(json.dumps({"x": {"out": 2}}))
    assert normalized_sha256(pa) != normalized_sha256(pb)


def test_normalized_digest_of_non_json_and_bad_json_is_the_byte_hash(tmp_path: Path) -> None:
    p = tmp_path / "t.parquet"
    p.write_bytes(b"PAR1")
    assert normalized_sha256(p) == sha256_file(p)
    q = tmp_path / "bad.json"
    q.write_text("{not json")
    assert normalized_sha256(q) == sha256_file(q)
    n = tmp_path / "nan.json"
    n.write_text('{"x": NaN}')  # not canonicalisable: compared as bytes
    assert normalized_sha256(n) == sha256_file(n)


def test_strip_volatile_does_not_mutate() -> None:
    doc = {"spec": {"out": "x"}, "timings": {}}
    strip_volatile(doc)
    assert doc == {"spec": {"out": "x"}, "timings": {}}


def test_json_pointer_rfc6901() -> None:
    doc = {"a": {"b/c": [10, {"~k": "v"}]}, "": 5}
    assert json_pointer(doc, "/a/b~1c/0") == 10
    assert json_pointer(doc, "/a/b~1c/1/~0k") == "v"
    assert json_pointer(doc, "/") == 5
    for bad in ("/a/x", "/a/b~1c/2", "/a/b~1c/01", "/a/b~1c/-1", "/a/b~1c/0/z"):
        with pytest.raises(KeyError):
            json_pointer(doc, bad)
    with pytest.raises(QtError):
        pointer_tokens("a/b")
    assert escape_token("a/b~c") == "a~1b~0c"
    assert pointer_tokens("/" + escape_token("a/b~c")) == ["a/b~c"]


def test_canonical_value_text() -> None:
    assert canonical_value_text("sim_overfills") == "sim_overfills"
    assert canonical_value_text(0.0) == "0"
    assert canonical_value_text(1e-7) == "1e-7"
    assert canonical_value_text({"b": 1, "a": [True, None]}) == '{"a":[true,null],"b":1}'
