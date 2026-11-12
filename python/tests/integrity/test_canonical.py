"""`queuetruth.integrity`: the fast RFC 8785 path against the reference rule, each named form, the old names, and a
cross-check of run ids and canonical JSON against the Rust implementation (`qt_schema`)."""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from queuetruth import integrity
from queuetruth.integrity import canonical_bytes, canonical_json, escape_string, format_float, utf16_key

REPO = Path(__file__).resolve().parents[3]
KAT = json.loads((REPO / "schemas" / "run_id_kat.json").read_text())


def reference_jcs(value: Any) -> str:
    """RFC 8785 written out with the Python escape loop (the rule `schemas.runid` used before the move)."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format_float(value)
    if isinstance(value, str):
        return escape_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(reference_jcs(v) for v in value) + "]"
    items = sorted(value.items(), key=lambda kv: utf16_key(kv[0]))
    return "{" + ",".join(escape_string(k) + ":" + reference_jcs(v) for k, v in items) + "}"


def test_jcs_matches_the_reference_rule() -> None:
    docs: list[object] = [c["spec"] for c in KAT["cases"]]
    docs += [json.loads(text) for text, _ in KAT["numbers"]]
    every_char = "".join(chr(c) for c in range(0x10000) if not 0xD800 <= c < 0xE000) + "\U0001f600\U00010000"
    docs += [
        every_char,
        {every_char[i : i + 7]: i for i in range(0, 2000, 7)},
        {"": 1, "\U0001f600": 2, "a": [None, True, False, 0, -1, 1e-7, 1e21, 0.1, 2**70]},
        [{"z": {"y": [1.5, "x\u0000\u001f\u007f"]}}],
    ]
    rng = np.random.default_rng(1)
    for _ in range(200):
        docs.append({"p": float(rng.random()), "n": int(rng.integers(-(10**12), 10**12)), "s": rng.bytes(8).hex()})
    for d in docs:
        assert canonical_json(d) == reference_jcs(d)
    with pytest.raises(TypeError):
        canonical_json({1: 2})
    with pytest.raises(TypeError):
        canonical_json({"a": object()})
    with pytest.raises(ValueError):
        canonical_json(float("inf"))


def test_forms_differ_only_where_documented() -> None:
    doc = {"b": [-0.0, 1e-7, "é"], "a": 1}
    assert canonical_json(doc) == '{"a":1,"b":[0,1e-7,"é"]}'
    assert canonical_json(doc, "compact") == '{"a":1,"b":[-0.0,1e-07,"é"]}'
    assert canonical_json(doc, "atlas-v1") == '{"a":1,"b":[-0.0,1e-07,"é"]}'
    assert canonical_json(doc, "compact-ascii") == '{"a":1,"b":[-0.0,1e-07,"\\u00e9"]}'
    assert canonical_json(doc, "sorted") == '{"a": 1, "b": [-0.0, 1e-07, "\\u00e9"]}'
    assert canonical_bytes(doc, "atlas-v1") == canonical_json(doc, "atlas-v1").encode("utf-8")
    assert canonical_json({"x": float("nan")}, "compact") == '{"x":NaN}'
    with pytest.raises(ValueError):
        canonical_json({"x": [float("nan")]}, "atlas-v1")
    with pytest.raises(ValueError):
        canonical_json(1, "pretty")  # type: ignore[arg-type]


def test_digests(tmp_path: Path) -> None:
    p = tmp_path / "f.bin"
    data = bytes(range(256)) * 9000  # more than one chunk
    p.write_bytes(data)
    want = integrity.sha256_bytes(data)
    assert integrity.sha256_file(p) == want
    assert integrity.sha256_file(p, chunk=7) == want
    with p.open("rb") as f:
        assert integrity.sha256_stream(f) == want
    assert integrity.sha256_text("é") == integrity.sha256_bytes("é".encode())
    assert integrity.canonical_sha256({"a": 1}, "sorted") == integrity.sha256_text('{"a": 1}')
    with pytest.raises(OSError):
        integrity.sha256_file(tmp_path / "missing")


def test_old_names_still_work_with_a_warning() -> None:
    from queuetruth.schemas import runid

    for get in (lambda: runid.canonical_json, lambda: __import__("queuetruth.schemas").schemas.canonical_json):
        with pytest.warns(DeprecationWarning, match=r"queuetruth\.integrity\.canonical_json"):
            fn = get()
        assert fn is canonical_json
    with pytest.warns(DeprecationWarning):
        assert runid._format_float(1e21) == "1e+21"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert runid.run_id({"seed": 1}, "0.1.0", lambda r: "") == runid.run_id({"seed": 1}, "0.1.0", lambda r: "")


def test_run_id_matches_rust(tmp_path: Path) -> None:
    """Python and Rust compute the same run id for every known-answer spec (references made local files)."""
    core = pytest.importorskip("queuetruth._core")
    from queuetruth.schemas.runid import is_ref, run_id

    engine = core.version()
    files: dict[str, str] = {}

    def local(v: Any) -> Any:
        if is_ref(v):
            key = canonical_json(v)
            if key not in files:
                f = tmp_path / f"ref{len(files)}.bin"
                f.write_bytes(key.encode())
                files[key] = str(f)
            return {"path": files[key]}
        if isinstance(v, dict):
            return {k: local(x) for k, x in v.items()}
        if isinstance(v, list):
            return [local(x) for x in v]
        return v

    checked = 0
    for case in KAT["cases"]:
        spec = local(case["spec"])
        want = run_id(spec, engine, lambda r: integrity.sha256_file(r["path"]))
        assert core.run_id(json.dumps(spec)) == want, case["name"]
        checked += 1
    assert checked >= 4
