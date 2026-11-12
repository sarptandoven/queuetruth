"""Known answers for every integrity primitive: canonical JSON in each form, run ids, a certificate seal, a
commitment, the ledger, forecast and sandbox hash chains, and an atlas day manifest's Merkle roots built from
synthetic unit leaves. The answers were computed with the code as it was before `queuetruth.integrity` existed, so
these tests prove that the move changed no published byte. Never regenerate them: a failure here means a run id,
certificate, ledger entry, day root or atlas root that is already published would no longer reproduce."""

from __future__ import annotations

import json
from pathlib import Path

import common as K
import pyarrow as pa

from queuetruth.cert import commit as CM
from queuetruth.cert.certify import seal
from queuetruth.cert.hashing import canonical_value_text, normalized_sha256
from queuetruth.client.testing import _canonical
from queuetruth.forecast import ledger as FL
from queuetruth.ledger._store import encode_line
from queuetruth.sandbox.log import GENESIS, entry_hash
from queuetruth.schemas.runid import canonical_json, run_id

#: Synthetic document with every case the forms disagree on: key order by UTF-16 code units, a non-BMP key,
#: control characters, negative zero, large and small exponents, a 64-bit integer and nested values.
DOC = {
    "b": [1, 2.5, -0.0, 1e21, 1e-7, 123456789012345678, True, None],
    "a": {"é": "line\nbreak\t\u0001\u007f", "\U0001f600": "smile", "Z": "x"},
    "c": 0.1,
}
JCS = (
    '{"a":{"Z":"x","é":"line\\nbreak\\t\\u0001\u007f","\U0001f600":"smile"},'
    '"b":[1,2.5,0,1e+21,1e-7,123456789012345678,true,null],"c":0.1}'
)
COMPACT = (
    '{"a":{"Z":"x","é":"line\\nbreak\\t\\u0001\u007f","\U0001f600":"smile"},'
    '"b":[1,2.5,-0.0,1e+21,1e-07,123456789012345678,true,null],"c":0.1}'
)

CERT_BODY = {
    "schema": "qt.certificate",
    "schema_version": 1,
    "runs": [{"run_id": "abc", "outputs": [{"p": "x.json", "v": 0.25}]}],
    "n": 3,
}
CERT_SHA = "1a00810ad45c08e65a98a180a3215f3a3081cc76721a6ceae7406525315e8342"
NORMALIZED = "a9e838b4c48707185337a1d1366aabdd93883db48451a7661df76133ad700892"
COMMIT_LEAVES = [
    "b9a05c7673ca75ad921fb6165f7aa385d2f5461e9c25b1a449277509608f939e",
    "7aca36c3105caedf67f4823d75c2e940f5286a657534c0268c11eb5314a9af81",
]
COMMIT_ROOT = "976a76a442f40750e26a3e6a693b1a0cdea541e77fed883501b78405db781ec9"
LEDGER_HASHES = [
    "3a143b23fad0ebe9670238b96904dbbd3983abda5908812749b1049b9be6dd71",
    "54eee3e5d537d2158e45f72bda35a46501a1fcdd149e6806cff59104ad26142c",
    "3c95b776ac8b3d9a4c6d0cdef4a8ca5a472b9d72ca7b46d17f38b54e067856d7",
]
FORECAST = (
    "281de600bd83ec041fd0cd894f048b495be13b1a75680e255f938947ea500f61",
    "2c6576721601766d3abfa492847eb3c7ea7958501e64f2946c0e80b3e4faaf41",
)
SANDBOX = (
    "fcbbc48335e00270e74cd0370d4e230beabafea78180cd3b3bc5b2c610822c53",
    "397feeced29ec6ef08b03af4a1be445659a2c6b4ad934295c0908637a2101e95",
)
DAY2_LEAVES = [
    "26eb25675f58961c8b69b17c03276966358d66dd39be8d03b5115dbeb71c5c87",
    "f363b319a1deb03a8c0c6d2c6e46602880f6d390294fbdbc8305beadc856d02a",
    "00ae5dda055ca57a7c6f55b42fce6a7aacb86c33abfdd0df2ae0ffa8a289271e",
    "2f6453b7fcf90998da516862f2b7dca96b0ede02e8b2cad391b4dc00783ec5f9",
    "9651a6636f979251a8e1f201194c26151af49335929555a94e782d1d3725354b",
]
DAY2_PROOF_3 = [
    ["L", "00ae5dda055ca57a7c6f55b42fce6a7aacb86c33abfdd0df2ae0ffa8a289271e"],
    ["L", "45abfab54711672fa215c3200983ec88e95350d7f8f35458e8aa4be6569cca99"],
    ["R", "9651a6636f979251a8e1f201194c26151af49335929555a94e782d1d3725354b"],
]
DAY_ROOTS = [
    "91d16e98ef529a125c3d378eda847bd970e8211c9de2a0158a9f42f4e321afbe",
    "790452a0541177133c361466ec044ce231a65d8b6783324696dc96a6df1509d1",
    "074cf221b6d881496b20167499e52c1a3e59c698000a49109ec165e703a462cf",
]
ATLAS_ROOT = "394be00770def03a02128ec5fdc9db4bb69a5058ebff5ccbc5a0760ee63094bb"
EMPTY_ROOT = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
TABLE_DIGEST = "db37215c107c37047a5bd6376c77a86d4b73129e2b71f87d4b9071fad0c82c42"
UNIT = {"unit_id": "u0", "symbols": ["AAA", "BBB"], "date": "2020-01-02"}


def _day_leaves(d: int) -> list[str]:
    spec = K.sha256_hex(K.canonical_json(UNIT))
    return [K.leaf_hash({"unit_id": f"u{i}", "unit_spec_sha256": spec, "n": i, "d": d}) for i in range(d + 3)]


def test_canonical_json_forms() -> None:
    assert canonical_json(DOC) == JCS
    assert FL.canonical_json(DOC) == JCS
    assert _canonical(DOC) == COMPACT
    assert K.canonical_json(DOC) == COMPACT.encode("utf-8")
    assert canonical_value_text("plain") == "plain"
    assert canonical_value_text({"x": [1.0, 2]}) == '{"x":[1,2]}'


def test_run_id() -> None:
    spec = {"dataset": {"dataset_id": "d" * 64}, "seed": 7, "out": "x"}
    assert run_id(spec, "0.3.0", lambda r: "f" * 64) == "39f24e1b630b5ccf"


def test_certificate(tmp_path: Path) -> None:
    doc = seal(CERT_BODY, None)
    assert doc["integrity"]["body_sha256"] == CERT_SHA
    assert doc["certificate_id"] == CERT_SHA[:16]
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"timings": {"t": 1}, "spec": {"out": "o", "seed": 1}, "v": [0.5, 2]}))
    assert normalized_sha256(p) == NORMALIZED
    leaves = [CM.leaf("00" * 32, "a.txt", "11" * 32), CM.leaf("22" * 32, "b.txt", "33" * 32)]
    assert leaves == COMMIT_LEAVES
    assert CM.root_of(leaves) == COMMIT_ROOT


def test_ledger_chain() -> None:
    prev = "0" * 64
    for seq, want in enumerate(LEDGER_HASHES):
        line, h = encode_line({"seq": seq, "prev": prev, "value": seq * 0.5, "name": f"r{seq}"})
        assert h == want
        assert line.startswith(b'{"hash":"' + want.encode() + b'","record":{"name":"r')
        prev = h


def test_forecast_and_sandbox_chains() -> None:
    entry = {"seq": 0, "prev_sha256": "0" * 64, "payload": {"p": 0.62, "q": "Will it?"}}
    entry["payload_sha256"] = FL.payload_digest(entry["payload"])
    assert (entry["payload_sha256"], FL.entry_digest(entry)) == FORECAST
    assert GENESIS == SANDBOX[0]
    assert entry_hash({"seq": 0, "kind": "open", "data": {"x": 1.5}, "prev": GENESIS}) == SANDBOX[1]


def test_atlas_merkle() -> None:
    roots = []
    for d in range(3):
        leaves = _day_leaves(d)
        roots.append(K.merkle_root(leaves))
        for i, leaf in enumerate(leaves):
            assert K.verify_proof(leaf, K.merkle_proof(leaves, i), roots[-1])
    assert _day_leaves(2) == DAY2_LEAVES
    assert K.merkle_proof(DAY2_LEAVES, 3) == DAY2_PROOF_3
    assert roots == DAY_ROOTS
    assert K.merkle_root(roots) == ATLAS_ROOT
    assert K.merkle_root([]) == EMPTY_ROOT
    t = pa.table({"k": pa.array([2, 1], pa.int64()), "s": pa.array(["b", None]), "f": pa.array([0.5, 1.25])})
    assert K.table_digest(t, ["k"]) == TABLE_DIGEST
