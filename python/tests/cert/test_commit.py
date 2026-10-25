"""Hash commitments (`queuetruth commit` / `reveal`) and their link into certificates."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

from cert.helpers import failed, make_cert, schema_valid, status, verify, write
from queuetruth.cert import keys
from queuetruth.cert.commit import (
    commit,
    default_opening_path,
    leaf,
    load_doc,
    reveal,
    root_of,
    write_pair,
)
from queuetruth.cert.models import Commitment, CommitmentOpening
from queuetruth.errors import QtError


def _files(tmp_path: Path) -> list[Path]:
    a = tmp_path / "forecast.csv"
    a.write_text("question,p\nfed_cut_2026_12,0.62\n")
    b = tmp_path / "orders.csv"
    b.write_text("side,px,qty\nB,100,5\n")
    return [a, b]


def _pair(tmp_path: Path, files: list[Path], **kw) -> tuple[Path, Path]:  # type: ignore[no-untyped-def]
    c, o = commit(files, **kw)
    out = tmp_path / "commitment.json"
    write_pair(c, o, out, default_opening_path(out))
    return out, default_opening_path(out)


def test_leaf_and_root_known_answers() -> None:
    nonce = "11" * 32
    sha = hashlib.sha256(b"0.62").hexdigest()
    want = hashlib.sha256(
        b"qt-commit-v1\x00leaf\x00" + bytes.fromhex(nonce) + b"f.csv" + b"\x00" + bytes.fromhex(sha)
    ).hexdigest()
    assert leaf(nonce, "f.csv", sha) == want
    assert root_of([want, want]) == hashlib.sha256(b"qt-commit-v1\x00root\x00" + bytes.fromhex(want) * 2).hexdigest()
    with pytest.raises(QtError):
        leaf(nonce, "a\x00b", sha)


def test_commit_and_reveal_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1767225600")
    files = _files(tmp_path)
    out, opening = _pair(tmp_path, files, label="weekly forecasts")
    c, raw = load_doc(out, Commitment)
    schema_valid(raw)
    schema_valid(json.loads(opening.read_text()))
    assert c.committed_at == 1_767_225_600 * 10**9 and c.n_items == 2 and c.label == "weekly forecasts"
    assert stat.S_IMODE(opening.stat().st_mode) == 0o600
    # the public document names neither the files nor their hashes
    text = out.read_text()
    for f in files:
        assert f.name not in text and hashlib.sha256(f.read_bytes()).hexdigest() not in text
    ver = reveal(out, opening, files)
    schema_valid(ver.model_dump(mode="json"))
    assert ver.status == "pass"
    assert [(o.name, o.status) for o in ver.revealed] == [("forecast.csv", "match"), ("orders.csv", "match")]
    assert ver.unmatched_files == []
    assert status_of(ver, "signature") == "skip"


def status_of(ver, cid: str) -> str:  # type: ignore[no-untyped-def]
    return next(c.status for c in ver.checks if c.id == cid)


def test_commitments_hide_identical_content(tmp_path: Path) -> None:
    f = _files(tmp_path)[:1]
    c1, o1 = commit(f)
    c2, o2 = commit(f)
    assert o1.items[0].sha256 == o2.items[0].sha256
    assert c1.leaves != c2.leaves and c1.root != c2.root  # fresh nonces


def test_selective_reveal(tmp_path: Path) -> None:
    files = _files(tmp_path)
    out, opening = _pair(tmp_path, files)
    o, _ = load_doc(opening, CommitmentOpening)
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps(o.model_copy(update={"items": o.items[1:]}).model_dump(mode="json")))
    ver = reveal(out, partial, [files[1]])
    assert ver.status == "pass" and ver.n_revealed == 1 and ver.revealed[0].name == "orders.csv"
    # revealing only the opening (no files) checks the leaves only
    ver = reveal(out, partial, [])
    assert ver.status == "pass" and ver.revealed[0].status == "not_given" and status_of(ver, "content") == "skip"


def test_reveal_failures(tmp_path: Path) -> None:
    files = _files(tmp_path)
    out, opening = _pair(tmp_path, files)
    # changed content
    files[0].write_text("question,p\nfed_cut_2026_12,0.71\n")
    ver = reveal(out, opening, files)
    assert ver.status == "fail"
    assert ver.revealed[0].status == "content_mismatch" and ver.revealed[1].status == "match"
    assert ver.unmatched_files == [str(files[0])]
    # a forged opening (another nonce) does not open the leaf
    o = json.loads(opening.read_text())
    o["items"][1]["nonce"] = "00" * 32
    bad = tmp_path / "bad_opening.json"
    bad.write_text(json.dumps(o))
    ver = reveal(out, bad, [files[1]])
    assert ver.revealed[0].status != "leaf_mismatch"  # item 0 untouched (content changed above)
    assert ver.revealed[1].status == "leaf_mismatch" and status_of(ver, "opening") == "fail"
    # an opening of another commitment
    other_out = tmp_path / "other" / "c.json"
    c2, o2 = commit(files[1:])
    write_pair(c2, o2, other_out, default_opening_path(other_out))
    with pytest.raises(QtError, match="another commitment"):
        reveal(out, default_opening_path(other_out), files)
    # an empty opening opens nothing
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({**json.loads(opening.read_text()), "items": []}))
    assert status_of(reveal(out, empty, []), "opening") == "fail"
    # inconsistent commitment document
    raw = json.loads(out.read_text())
    raw["n_items"] = 3
    out.write_text(json.dumps(raw))
    assert status_of(reveal(out, opening, []), "structure") == "fail"


def test_signed_commitment(tmp_path: Path, keypair: tuple[Path, Path, str]) -> None:
    key, _ = keys.load_private(keypair[0])
    files = _files(tmp_path)
    out, opening = _pair(tmp_path, files, key=key)
    ver = reveal(out, opening, files, trusted={keypair[2]})
    assert ver.status == "pass" and ver.commitment.key_trusted is True
    assert status_of(ver, "signature") == "pass" and status_of(ver, "key_pin") == "pass"
    assert status_of(reveal(out, opening, files, trusted={"ab" * 32}), "key_pin") == "fail"
    raw = json.loads(out.read_text())
    raw["committed_at"] -= 86_400 * 10**9  # backdating breaks the signature
    out.write_text(json.dumps(raw))
    assert status_of(reveal(out, opening, files), "signature") == "fail"


def test_commit_usage_errors(tmp_path: Path) -> None:
    files = _files(tmp_path)
    with pytest.raises(QtError, match="at least one"):
        commit([])
    with pytest.raises(QtError, match="duplicate names"):
        commit(files, names=["x", "x"])
    with pytest.raises(QtError) as e:
        commit([tmp_path])
    assert e.value.code == "QT-INPUT-001"
    c, o = commit(files)
    with pytest.raises(QtError, match="different files"):
        write_pair(c, o, tmp_path / "same.json", tmp_path / "same.json")
    (tmp_path / "x.json").write_text('{"schema": "qt.certificate"}')
    with pytest.raises(QtError, match=r"expected a qt\.commitment"):
        load_doc(tmp_path / "x.json", Commitment)
    (tmp_path / "y.json").write_text('{"schema": "qt.commitment", "schema_version": 1}')
    with pytest.raises(QtError, match=r"not a valid qt\.commitment"):
        load_doc(tmp_path / "y.json", Commitment)
    assert default_opening_path(Path("a/c.json")) == Path("a/c.opening.json")
    assert default_opening_path(Path("a/c")) == Path("a/c.opening.json")


# ---- commitments linked into certificates -------------------------------------------------------------------------


@pytest.mark.requires_core
@pytest.mark.parametrize(("epoch", "precedes"), [(1_700_000_000, True), (1_800_000_000, False)])
def test_certificate_links_a_commitment(
    work: Path, monkeypatch: pytest.MonkeyPatch, epoch: int, precedes: bool
) -> None:
    """Shadows committed at 2023-11-14 (before the 2026-06-15 data) or 2027-01-15 (after it)."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(epoch))
    c, o = commit([work / "shadows.parquet"], label="shadow schedule")
    write_pair(c, o, work.parent / "c.json", work.parent / "o.json")
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1900000000")
    cert = make_cert(commitments=[(work.parent / "c.json", work.parent / "o.json")])
    link = cert.commitments[0]
    shadows_id = next(x.id for x in cert.inputs if x.path == "shadows.parquet")
    assert link.revealed[0].inputs == [shadows_id]
    ds = next(x for x in cert.inputs if x.kind == "dataset")
    assert link.earliest_data_ts == ds.data_ts_min
    assert link.precedes_data is precedes
    assert any("asserted by its committer" in w for w in cert.warnings)
    p = write(cert, work.parent / "cert.json")
    ver = verify(p)
    assert ver.status == "pass" and status(ver, "commitment:1") == "pass"
    # the commitment file changes after certification
    raw = json.loads((work.parent / "c.json").read_text())
    raw["label"] = "edited"
    (work.parent / "c.json").write_text(json.dumps(raw))
    assert failed(verify(p)) == ["commitment:1"]


@pytest.mark.requires_core
def test_certificate_refuses_a_bad_opening(work: Path) -> None:
    c, o = commit([work / "shadows.parquet"])
    o = o.model_copy(update={"items": [o.items[0].model_copy(update={"nonce": "22" * 32})]})
    write_pair(c, o, work.parent / "c.json", work.parent / "o.json")
    with pytest.raises(QtError, match="does not open its leaf"):
        make_cert(commitments=[(work.parent / "c.json", work.parent / "o.json")])


@pytest.mark.requires_core
def test_certificate_warns_when_no_revealed_item_is_an_input(work: Path, tmp_path: Path) -> None:
    other = tmp_path / "unrelated.txt"
    other.write_text("x")
    c, o = commit([other])
    write_pair(c, o, work.parent / "c.json", work.parent / "o.json")
    cert = make_cert(commitments=[(work.parent / "c.json", work.parent / "o.json")])
    assert cert.commitments[0].revealed[0].inputs == []
    assert cert.commitments[0].precedes_data is None
    assert any("no revealed item matches" in w for w in cert.warnings)
