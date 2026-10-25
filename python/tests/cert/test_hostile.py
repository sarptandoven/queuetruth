"""Certificates written by someone else: every field a forger controls is checked before it is used, and verifying
never deletes, overwrites or imports anything of the verifier's."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cert.helpers import failed, load, make_cert, reseal, status, verify, write
from queuetruth.cert import keys
from queuetruth.cert.certify import certify
from queuetruth.cert.commit import commit, write_pair
from queuetruth.cert.hashing import normalized_sha256
from queuetruth.errors import QtError

pytestmark = pytest.mark.requires_core


def _forge(work: Path, edit, runs: tuple[str, ...] = ("replay",), **kw) -> Path:  # type: ignore[no-untyped-def]
    doc = make_cert(runs, **kw).model_dump(mode="json")
    edit(doc)
    return write(reseal(doc), work.parent / "forged.json")


# ---- labels and paths ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["..", ".", "/abs/path", "a/b", ".hidden", "x" * 65, ""])
def test_hostile_run_labels_are_refused_before_anything_runs(work: Path, tmp_path: Path, label: str) -> None:
    keep = tmp_path / "parent" / "child"
    keep.mkdir(parents=True)
    (keep.parent / "precious.txt").write_text("keep me")

    def edit(doc: dict) -> None:
        doc["runs"][0]["label"] = label

    p = _forge(work, edit, claims=[])
    ver = verify(p, rerun=True, work=keep / "w")
    assert ver.status == "fail" and failed(ver) == ["schema"]
    assert [c.id for c in ver.checks] == ["schema"]  # no rerun, no file touched
    assert (keep.parent / "precious.txt").read_text() == "keep me"
    assert not (keep / "w").exists()


def test_hostile_file_paths_are_refused(work: Path) -> None:
    def outside(doc: dict) -> None:
        doc["claims"][0]["evidence"][0]["file"] = "../shadows.parquet"

    assert failed(verify(_forge(work, outside, claims=["{replay#/shadows/n}"]))) == ["schema"]

    def absolute(doc: dict) -> None:
        doc["runs"][0]["outputs"][0]["path"] = str(work / "shadows.parquet")

    assert failed(verify(_forge(work, absolute, claims=[]))) == ["schema"]


def test_duplicate_labels_and_duplicate_json_keys(work: Path) -> None:
    def dup(doc: dict) -> None:
        doc["runs"].append(json.loads(json.dumps(doc["runs"][0])))

    ver = verify(_forge(work, dup, claims=[]))
    assert failed(ver) == ["schema"] and "appears 2 times" in ver.checks[0].details[0]
    p = write(make_cert(("replay",), claims=[]), work.parent / "c.json")
    text = p.read_text()
    p.write_text(text.replace('"title": null,', '"title": null, "title": "second",', 1))
    with pytest.raises(QtError, match="duplicate object key"):
        verify(p)


# ---- --work never replaces anything ---------------------------------------------------------------------------------


def test_work_must_be_new_or_empty_and_apart_from_certified_paths(work: Path, tmp_path: Path) -> None:
    p = write(make_cert(("replay",), claims=[]), work.parent / "cert.json")
    (work / "replay" / "mine.txt").write_text("user file")
    summary = (work / "replay" / "summary.json").read_bytes()
    for target in (work, work / "replay", work / "fx", work.parent):
        with pytest.raises(QtError, match=r"must be a new or empty directory|overlaps the certified path"):
            verify(p, rerun=True, work=target)
    empty_input = work / "fx" / "dataset" / "empty_dir"
    empty_input.mkdir()
    with pytest.raises(QtError, match="overlaps the certified path fx/dataset"):
        verify(p, rerun=True, work=empty_input)
    assert (work / "replay" / "mine.txt").read_text() == "user file"
    assert (work / "replay" / "summary.json").read_bytes() == summary
    # a new directory is created and filled; it is not removed afterwards
    ver = verify(p, rerun=True, work=tmp_path / "new" / "w")
    assert ver.status == "pass", failed(ver)
    assert (tmp_path / "new" / "w" / "replay" / "summary.json").is_file()


# ---- claims ---------------------------------------------------------------------------------------------------------


def test_forged_claim_evidence_fails(work: Path) -> None:
    claim = ["replay filled {replay#/shadows/n_filled} of {replay#/shadows/n} shadows"]

    def repoint(doc: dict) -> None:
        ev = doc["claims"][0]["evidence"][0]
        ev["pointer"] = "/shadows/n"
        ev["value"] = doc["claims"][0]["evidence"][1]["value"]
        n = ev["value"]
        doc["claims"][0]["rendered"] = f"replay filled {n} of {n} shadows"

    for rerun in (False, True):
        ver = verify(_forge(work, repoint, claims=claim), rerun=rerun)
        assert status(ver, "claim:c1") == "fail", rerun
        assert "is not the reference of the text" in next(c for c in ver.checks if c.id == "claim:c1").details[0]

    def other_file(doc: dict) -> None:
        ev = doc["claims"][0]["evidence"][0]
        ev["file"] = "data_quality.json"

    assert "claim:c1" in failed(verify(_forge(work, other_file, claims=claim)))

    def uncertified_output(doc: dict) -> None:
        run = doc["runs"][0]
        run["outputs"] = [o for o in run["outputs"] if o["path"] != "summary.json"]

    ver = verify(_forge(work, uncertified_output, claims=claim))
    assert "not a certified output" in next(c for c in ver.checks if c.id == "claim:c1").details[0]


def test_claim_on_an_output_that_failed_its_hash_check_fails(work: Path) -> None:
    p = write(make_cert(("replay",), claims=["{replay#/shadows/n}"]), work.parent / "c.json")
    s = json.loads((work / "replay/summary.json").read_text())
    s["stats"] = {"edited": True}
    (work / "replay/summary.json").write_text(json.dumps(s))
    ver = verify(p)
    assert sorted(failed(ver)) == ["claim:c1", "outputs:replay"]
    assert "did not pass its hash check" in next(c for c in ver.checks if c.id == "claim:c1").details[0]


# ---- outputs: the normalized digest is recomputed --------------------------------------------------------------------


def test_swapped_normalized_digest_is_caught(work: Path) -> None:
    honest = normalized_sha256(work / "replay" / "summary.json")
    s = json.loads((work / "replay/summary.json").read_text())
    s["shadows"]["n_filled"] = s["shadows"]["n"]
    (work / "replay/summary.json").write_text(json.dumps(s, indent=2))

    def swap(doc: dict) -> None:
        for o in doc["runs"][0]["outputs"]:
            if o["path"] == "summary.json":
                o["normalized_sha256"] = honest

    p = _forge(work, swap, claims=[])
    ver = verify(p)
    assert failed(ver) == ["outputs:replay"]
    details = next(c for c in ver.checks if c.id == "outputs:replay").details
    assert details == [f"normalized sha256 of summary.json does not recompute: certified {honest}"]
    ver = verify(p, rerun=True)
    assert sorted(failed(ver)) == ["outputs:replay", "rerun:replay"]
    assert ver.runs[0].differing == ["summary.json"]


# ---- binding --------------------------------------------------------------------------------------------------------


def test_binding_checks_kind_and_input_paths(work: Path) -> None:
    def kind(doc: dict) -> None:
        doc["runs"][0]["kind"] = "score"

    assert "binding:replay" in failed(verify(_forge(work, kind, claims=[])))

    def swap_inputs(doc: dict) -> None:
        a, b = doc["runs"][0]["inputs"]
        a["input"], b["input"] = b["input"], a["input"]

    ver = verify(_forge(work, swap_inputs, claims=[]))
    details = next(c for c in ver.checks if c.id == "binding:replay").details
    assert any("its certified input is" in d for d in details)

    def size(doc: dict) -> None:
        doc["runs"][0]["outputs"][0]["bytes"] += 1

    assert failed(verify(_forge(work, size, claims=[]))) == ["outputs:replay"]


# ---- commitments ----------------------------------------------------------------------------------------------------


def _committed(work: Path, files: list[Path], monkeypatch: pytest.MonkeyPatch, epoch: int) -> list[tuple[Path, Path]]:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(epoch))
    c, o = commit(files)
    write_pair(c, o, work.parent / "c.json", work.parent / "o.json")
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1900000000")
    return [(work.parent / "c.json", work.parent / "o.json")]


def test_forged_commitment_links_fail(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (work / "dummy.txt").write_text("unrelated")
    links = _committed(work, [work / "dummy.txt"], monkeypatch, 1_700_000_000)

    def relink(doc: dict) -> None:
        shadows = next(x["id"] for x in doc["inputs"] if x["path"] == "shadows.parquet")
        ds = next(x for x in doc["inputs"] if x["kind"] == "dataset")
        link = doc["commitments"][0]
        link["revealed"][0]["inputs"] = [shadows]
        link["earliest_data_ts"] = ds["data_ts_min"]
        link["precedes_data"] = link["committed_at"] < ds["data_ts_min"]

    p = _forge(work, relink, claims=[], commitments=links)
    ver = verify(p)
    assert failed(ver) == ["commitment:1"]
    assert any("do not recompute from the certified file hashes" in d for d in ver.checks[-1].details)


def test_forged_data_span_fails(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    links = _committed(work, [work / "shadows.parquet"], monkeypatch, 1_900_000_000)  # 2030: after the data
    assert make_cert(("replay",), claims=[], commitments=links).commitments[0].precedes_data is False

    def later_data(doc: dict) -> None:
        ds = next(x for x in doc["inputs"] if x["kind"] == "dataset")
        ds["data_ts_min"] = 1_950_000_000 * 10**9
        ds["data_ts_max"] = 1_950_000_001 * 10**9
        doc["commitments"][0]["earliest_data_ts"] = ds["data_ts_min"]
        doc["commitments"][0]["precedes_data"] = True

    ver = verify(_forge(work, later_data, claims=[], commitments=links))
    assert "commitment:1" not in failed(ver)  # the link recomputes from the forged span ...
    ds_check = next(c for c in ver.checks if c.id.startswith("input:") and "fx/dataset" in c.summary)
    assert ds_check.status == "fail" and any("does not re-derive" in d for d in ds_check.details)  # ... which fails


def test_certify_refuses_an_inconsistent_commitment(work: Path) -> None:
    c, o = commit([work / "shadows.parquet"])
    write_pair(c.model_copy(update={"n_items": 2}), o, work.parent / "c.json", work.parent / "o.json")
    with pytest.raises(QtError, match="internally inconsistent"):
        certify(["replay"], commitments=[(work.parent / "c.json", work.parent / "o.json")])


# ---- signatures -----------------------------------------------------------------------------------------------------


def test_key_trusted_only_with_a_valid_signature(work: Path, keypair: tuple[Path, Path, str]) -> None:
    key, _ = keys.load_private(keypair[0])
    doc = make_cert(("replay",), claims=[], key=key).model_dump(mode="json")
    doc["title"] = "edited after signing"
    sealed = reseal(doc)
    sealed["integrity"]["signature"] = doc["integrity"]["signature"]  # stale signature, trusted fingerprint
    p = write(sealed, work.parent / "stale.json")
    ver = verify(p, trust=[keypair[2]])
    assert status(ver, "signature") == "fail" and status(ver, "key_pin") == "fail"
    assert ver.certificate.key_trusted is False
    assert "does not verify" in next(c.summary for c in ver.checks if c.id == "key_pin")


# ---- re-execution ---------------------------------------------------------------------------------------------------


def test_rerun_does_not_import_from_the_base_directory(work: Path, tmp_path: Path) -> None:
    marker = tmp_path / "imported"
    (work / "json.py").write_text(f"open({str(marker)!r}, 'w').write('x')\nfrom _json import *\n")
    p = write(make_cert(("referee",), claims=[]), work.parent / "c.json")
    ver = verify(p, rerun=True, only=["referee"])
    assert not marker.exists()
    assert status(ver, "rerun:referee") == "pass", failed(ver)


def test_rerun_is_skipped_after_an_integrity_failure(work: Path) -> None:
    doc = make_cert(("referee",), claims=[]).model_dump(mode="json")
    doc["title"] = "edited"
    p = write(doc, work.parent / "edited.json")
    ver = verify(p, rerun=True)
    assert status(ver, "rerun:referee") == "skip" and ver.runs[0].rerun == "skipped"
    assert "integrity failed" in next(c.summary for c in ver.checks if c.id == "rerun:referee")
    ver = verify(p, rerun=True, force_rerun=True)
    assert status(ver, "rerun:referee") == "pass" and failed(ver) == ["integrity"]


def test_rerun_with_map_reproduces_absolute_paths(work: Path, tmp_path: Path) -> None:
    from queuetruth import api

    api.dq({"dataset": {"path": str(work / "fx/dataset")}, "out": str(work / "abs_dq")})
    p = write(make_cert(("abs_dq",), claims=["dq status {abs_dq#/status}"]), tmp_path / "abs.json")
    moved = tmp_path / "moved"
    shutil.copytree(work, moved)
    shutil.rmtree(work)
    ver = verify(p, base=moved, mapping=[(str(work), str(moved))], rerun=True, work=tmp_path / "w")
    assert ver.status == "pass", [(c.id, c.details) for c in ver.checks if c.status == "fail"]
    assert ver.runs[0].rerun == "identical"
    # the re-executed report names the mapped location; it is compared as if it named the certified one
    assert str(moved) in (tmp_path / "w" / "abs_dq" / "dq_report.json").read_text()
    assert load(p)["runs"][0]["spec"]["dataset"]["path"] == str(work / "fx/dataset")


def test_rerun_strips_execution_only_fields_at_every_depth(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A nested `out` or `threads` in a spec echo (which SPEC 7.3 leaves out of the run id) never reaches the
    re-executed command: it could send writes elsewhere."""
    s = json.loads((work / "replay/summary.json").read_text())
    s["spec"]["latency"]["out"] = str(work.parent / "elsewhere")
    s["spec"]["latency"]["threads"] = 64
    (work / "replay/summary.json").write_text(json.dumps(s, indent=2))
    p = write(make_cert(("replay",), claims=[]), work.parent / "c.json")
    from queuetruth.cert import verify as qverify

    seen: list[dict] = []
    real = qverify.subprocess.run

    def spy(cmd: list[str], **kw):  # type: ignore[no-untyped-def]
        seen.append(json.loads(kw["input"]))
        return real(cmd, **kw)

    monkeypatch.setattr(qverify.subprocess, "run", spy)
    verify(p, rerun=True, threads=2)
    spec = seen[0]["spec"]
    assert "out" not in spec["latency"] and "threads" not in spec["latency"]
    assert spec["threads"] == 2 and seen[0]["out"].endswith("/replay")
    assert not (work.parent / "elsewhere").exists()
