"""`certify` and `verify-cert` without re-execution: round trips, signatures, tampering and refusals."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cert.helpers import CLAIMS, failed, load, make_cert, reseal, schema_valid, status, verify, write
from queuetruth.cert import keys
from queuetruth.cert.certify import certify, dataset_id
from queuetruth.cert.commit import commit, write_pair
from queuetruth.cert.hashing import sha256_bytes, sha256_file
from queuetruth.errors import QtError
from queuetruth.schemas.runid import canonical_json

pytestmark = pytest.mark.requires_core


def test_round_trip_unsigned(work: Path) -> None:
    cert = make_cert()
    doc = cert.model_dump(mode="json")
    schema_valid(doc)
    body = {k: v for k, v in doc.items() if k not in ("certificate_id", "integrity")}
    digest = sha256_bytes(canonical_json(body).encode())
    assert doc["integrity"]["body_sha256"] == digest
    assert doc["certificate_id"] == digest[:16]
    assert doc["integrity"]["signature"] is None
    assert doc["issued_at"] == 1_700_000_000 * 10**9  # SOURCE_DATE_EPOCH
    p = write(cert, work.parent / "cert.json")
    assert load(p) == doc  # the written file is the sealed document
    ver = verify(p)
    schema_valid(ver.model_dump(mode="json"))
    assert ver.status == "pass", failed(ver)
    assert status(ver, "integrity") == "pass"
    assert status(ver, "signature") == "warn"
    assert not any(c.id == "key_pin" for c in ver.checks)
    assert [(r.label, r.inputs_ok, r.outputs_ok, r.binding_ok, r.rerun) for r in ver.runs] == [
        ("replay", True, True, True, "not_run"),
        ("referee", True, True, True, "not_run"),
        ("dq", True, True, True, "not_run"),
    ]


def test_runs_inputs_and_claims_are_recorded_exactly(work: Path) -> None:
    cert = make_cert()
    summary = json.loads((work / "replay" / "summary.json").read_text())
    report = json.loads((work / "referee" / "referee_report.json").read_text())
    by = {r.label: r for r in cert.runs}
    assert by["replay"].run_id == summary["run_id"] and by["replay"].binding == "verified"
    assert by["referee"].binding == "recomputed" and by["dq"].binding == "recomputed"
    assert by["replay"].spec == summary["spec"]
    assert {o.path for o in by["replay"].outputs} == {p.name for p in (work / "replay").iterdir()}
    for o in by["replay"].outputs:
        assert o.sha256 == sha256_file(work / "replay" / o.path)
    # the dataset is one input shared by the three runs; its manifest re-derives its dataset_id
    datasets = [x for x in cert.inputs if x.kind == "dataset"]
    assert len(datasets) == 1 and datasets[0].path == "fx/dataset"
    ds = datasets[0]
    manifest = json.loads((work / "fx/dataset/manifest.json").read_text())
    shas = [f["sha256"] for p in manifest["partitions"] for f in p["files"]]
    assert ds.identity == ds.dataset_id == manifest["dataset_id"] == dataset_id(shas, manifest["source_format"])
    assert ds.dataset_consistent is True
    assert ds.data_ts_min == min(p["ts_min"] for p in manifest["partitions"])
    assert {ri.pointer for ri in by["replay"].inputs} == {"/dataset", "/shadows"}
    assert {ri.pointer for ri in by["referee"].inputs} == {"/dataset", "/schedule", "/sim_fills"}
    c1, c2, c3 = cert.claims
    assert c1.text == CLAIMS[0]
    assert [e.value for e in c1.evidence] == [summary["shadows"]["n_filled"], summary["shadows"]["n"]]
    assert c1.rendered == f"replay filled {summary['shadows']['n_filled']} of {summary['shadows']['n']} shadows"
    assert c2.evidence[0].value is report["pass"] and c2.evidence[1].file == "referee_report.json"
    assert c3.evidence[0].file == "dq_report.json"
    assert "records no run_id" in " ".join(cert.warnings)


def test_signed_certificate_and_key_pinning(work: Path, keypair: tuple[Path, Path, str], tmp_path: Path) -> None:
    key, _ = keys.load_private(keypair[0])
    cert = make_cert(key=key)
    sig = cert.integrity.signature
    assert sig is not None and sig.key_fingerprint == keypair[2]
    assert any(keypair[2] in line for line in cert.reproduce)
    p = write(cert, work.parent / "signed.json")
    ver = verify(p)
    assert ver.status == "pass"
    assert status(ver, "signature") == "pass" and status(ver, "key_pin") == "warn"
    assert ver.certificate.key_trusted is None
    ver = verify(p, trust=[f"sha256:{keypair[2].upper()}"])
    assert ver.status == "pass" and status(ver, "key_pin") == "pass" and ver.certificate.key_trusted is True
    ver = verify(p, pubkeys=[keypair[1]])
    assert status(ver, "key_pin") == "pass"
    other, _, _ = keys.generate(tmp_path / "other.pem")
    ver = verify(p, trust=[other.fingerprint])
    assert ver.status == "fail" and failed(ver) == ["key_pin"] and ver.certificate.key_trusted is False
    assert verify(p, require_signature=True).status == "pass"


def test_edited_certificate_fails_integrity(work: Path) -> None:
    doc = make_cert().model_dump(mode="json")
    doc["claims"][0]["rendered"] = "replay filled 20 of 20 shadows"
    ver = verify(write(doc, work.parent / "edited.json"))
    assert ver.status == "fail"
    assert status(ver, "integrity") == "fail"
    assert status(ver, "claim:c1") == "fail"  # the rendered text no longer follows from the values


def test_resealed_unsigned_forgery_is_caught_by_the_files(work: Path) -> None:
    """An unsigned digest is recomputable: integrity passes, but the cited value no longer matches the output."""
    doc = make_cert().model_dump(mode="json")
    doc["claims"][0]["evidence"][0]["value"] = 20
    doc["claims"][0]["rendered"] = "replay filled 20 of 20 shadows"
    ver = verify(write(reseal(doc), work.parent / "forged.json"))
    assert status(ver, "integrity") == "pass"
    assert failed(ver) == ["claim:c1"]


def test_resealed_signed_forgery(work: Path, keypair: tuple[Path, Path, str], tmp_path: Path) -> None:
    key, _ = keys.load_private(keypair[0])
    doc = make_cert(key=key).model_dump(mode="json")
    doc["title"] = "edited after signing"
    # unsigned re-seal: the signature is gone
    p = write(reseal(doc), work.parent / "unsigned.json")
    assert verify(p).status == "pass"  # files and claims still hold; only the provenance is lost
    assert status(verify(p), "signature") == "warn"
    assert failed(verify(p, require_signature=True)) == ["signature"]
    assert failed(verify(p, trust=[keypair[2]])) == ["key_pin"]
    # re-signed with the forger's own key: valid signature, wrong signer
    forger, _, _ = keys.generate(tmp_path / "forger.pem")
    p = write(reseal(doc, forger), work.parent / "resigned.json")
    ver = verify(p, trust=[keypair[2]])
    assert status(ver, "signature") == "pass"
    assert failed(ver) == ["key_pin"]
    # keeping the original signature over an edited body
    orig = make_cert(key=key).model_dump(mode="json")
    sig = orig["integrity"]["signature"]
    edited = reseal({**orig, "title": "edited"})
    edited["integrity"]["signature"] = sig
    ver = verify(write(edited, work.parent / "stale_sig.json"))
    assert failed(ver) == ["signature"]


def test_tampered_output_fails_outputs_and_claims(work: Path) -> None:
    p = write(make_cert(), work.parent / "cert.json")
    s = json.loads((work / "replay/summary.json").read_text())
    s["shadows"]["n_filled"] += 1
    (work / "replay/summary.json").write_text(json.dumps(s, indent=2))
    ver = verify(p)
    assert sorted(failed(ver)) == ["claim:c1", "outputs:replay"]
    assert any("changed: summary.json" in d for c in ver.checks for d in c.details)
    run = next(r for r in ver.runs if r.label == "replay")
    assert not run.outputs_ok and run.binding_ok


def test_tampered_input_fails_input_and_binding(work: Path) -> None:
    p = write(make_cert(), work.parent / "cert.json")
    with (work / "shadows.parquet").open("ab") as f:
        f.write(b"\0")
    ver = verify(p)
    shadows = next(c for c in ver.checks if c.id.startswith("input:") and "shadows.parquet" in c.summary)
    assert shadows.status == "fail"
    assert status(ver, "binding:replay") == "fail"
    assert status(ver, "binding:referee") == "pass"  # does not use the shadows
    assert not next(r for r in ver.runs if r.label == "replay").inputs_ok


def test_tampered_dataset_partition(work: Path) -> None:
    p = write(make_cert(), work.parent / "cert.json")
    part = sorted((work / "fx/dataset" / "mbo").rglob("*.parquet"))[0]
    part.write_bytes(part.read_bytes() + b"x")
    ver = verify(p)
    ds = next(c for c in ver.checks if c.id == "input:in1")
    assert ds.status == "fail"
    assert any("changed:" in d for d in ds.details)
    assert any("sha256 differs from manifest.json" in d for d in ds.details)


def test_extra_files_warn_and_missing_files_fail(work: Path) -> None:
    p = write(make_cert(), work.parent / "cert.json")
    (work / "replay" / "notes.txt").write_text("added later")
    ver = verify(p)
    assert ver.status == "pass" and status(ver, "outputs:replay") == "warn"
    (work / "referee" / "referee_report.json").unlink()
    ver = verify(p)
    assert "outputs:referee" in failed(ver)
    assert "binding:referee" in failed(ver)


def test_moved_tree_verifies_with_base(work: Path, tmp_path: Path) -> None:
    p = write(make_cert(), work.parent / "cert.json")
    moved = tmp_path / "elsewhere" / "tree"
    shutil.copytree(work, moved)
    assert verify(p, base=moved).status == "pass"
    ver = verify(p, base=tmp_path)
    assert ver.status == "fail"
    assert all(c.status == "fail" for c in ver.checks if c.id.startswith(("input:", "outputs:", "binding:")))


def test_absolute_paths_and_map(work: Path, tmp_path: Path) -> None:
    from queuetruth import api

    api.replay({"dataset": {"path": str(work / "fx/dataset")}, "out": str(work / "abs_replay")})
    cert = make_cert(("abs_replay",), claims=[])
    assert cert.inputs[0].path == str(work / "fx/dataset")
    p = write(cert, tmp_path / "abs.json")
    moved = tmp_path / "moved"
    shutil.copytree(work, moved)
    shutil.rmtree(work / "fx")
    assert verify(p, base=moved).status == "fail"
    ver = verify(p, base=moved, mapping=[(str(work), str(moved))])
    assert ver.status == "pass", failed(ver)


def test_attachments(work: Path) -> None:
    (work / "notes").mkdir()
    (work / "notes" / "a.txt").write_text("strategy notes")
    (work / "report.html").write_text("<html></html>")
    cert = make_cert(("replay",), claims=[], attach=["notes", "report.html"])
    assert [(a.id, a.kind, a.path) for a in cert.attachments] == [
        ("att1", "directory", "notes"),
        ("att2", "file", "report.html"),
    ]
    p = write(cert, work.parent / "att.json")
    assert verify(p).status == "pass"
    (work / "notes" / "a.txt").write_text("edited")
    assert failed(verify(p)) == ["attachment:att1"]


def test_only_limits_runs_inputs_and_claims(work: Path) -> None:
    p = write(make_cert(), work.parent / "cert.json")
    ver = verify(p, only=["referee"])
    assert [r.label for r in ver.runs] == ["referee"]
    assert not any(c.id == "input:in2" for c in ver.checks)  # shadows: replay only
    assert status(ver, "claim:c1") == "skip" and status(ver, "claim:c2") == "pass"
    with pytest.raises(QtError) as e:
        verify(p, only=["nope"])
    assert e.value.code == "QT-USAGE-001"


# ---- refusals -------------------------------------------------------------------------------------------------------


def _code(fn, *a, **kw) -> tuple[str, str, str | None]:  # type: ignore[no-untyped-def]
    with pytest.raises(QtError) as e:
        fn(*a, **kw)
    return e.value.code, e.value.message, e.value.hint


def test_refuses_non_run_directories(work: Path) -> None:
    code, msg, hint = _code(certify, ["fx"])
    assert code == "QT-INPUT-002" and "not a queuetruth run directory" in msg and "--attach" in (hint or "")
    assert _code(certify, ["missing"])[0] == "QT-INPUT-001"
    (work / "odd").mkdir()
    (work / "odd" / "summary.json").write_text('{"schema": "qt.other"}')
    code, msg, _ = _code(certify, ["odd"])
    assert code == "QT-INPUT-002" and "summary.json: qt.other" in msg
    (work / "odd" / "summary.json").write_text("{nope")
    assert _code(certify, ["odd"])[0] == "QT-INPUT-002"


def test_refuses_wrong_base(work: Path) -> None:
    code, _, hint = _code(certify, [str(work / "replay")], base=work / "replay")
    assert code == "QT-INPUT-001"
    assert "--base" in (hint or "")


def test_refuses_inputs_changed_after_the_run(work: Path) -> None:
    with (work / "shadows.parquet").open("ab") as f:
        f.write(b"\0")
    code, msg, hint = _code(certify, ["replay"])
    assert code == "QT-INPUT-002"
    assert "do not reproduce run_id" in msg and "another engine version" in (hint or "")


def test_refuses_dataset_inconsistent_with_its_manifest(work: Path) -> None:
    """A changed partition file leaves dataset_id (read from the manifest) and so run_id unchanged: only the
    manifest consistency check catches it."""
    part = sorted((work / "fx/dataset" / "mbo").rglob("*.parquet"))[0]
    part.write_bytes(part.read_bytes() + b"x")
    code, msg, _ = _code(certify, ["replay"])
    assert code == "QT-INPUT-002" and "inconsistent with their manifests" in msg


def test_refuses_bad_claims(work: Path) -> None:
    cases = {
        "{nope#/x}": "names run 'nope'",
        "{replay#/stats/does_not_exist}": "no value at /stats/does_not_exist",
        "{replay#/timings/wall_s}": "wall-clock",
        "{replay:missing.json#/x}": "not an output file",
        "{replay:bbo.parquet#/x}": "not a JSON file",
        "   ": "is empty",
    }
    for text, want in cases.items():
        code, msg, _ = _code(certify, ["replay"], claims=[text])
        assert code == "QT-USAGE-001" and want in msg, (text, msg)


def test_refuses_outputs_inside_runs_or_inputs(work: Path) -> None:
    code, msg, _ = _code(certify, ["replay"], out_files=[work / "replay" / "cert.json"])
    assert code == "QT-USAGE-001" and "inside the certified run directory" in msg
    code, msg, _ = _code(certify, ["replay"], out_files=[work / "fx" / "dataset" / "cert.json"])
    assert code == "QT-USAGE-001" and "would overwrite the certified input fx/dataset" in msg
    # a certified input file, attachment, commitment or opening is never overwritten either
    before = (work / "shadows.parquet").read_bytes()
    code, msg, _ = _code(certify, ["replay"], out_files=[work / "shadows.parquet"])
    assert code == "QT-USAGE-001" and "would overwrite the certified input shadows.parquet" in msg
    (work / "notes.txt").write_text("n")
    code, msg, _ = _code(certify, ["replay"], attach=["notes.txt"], out_files=[work / "notes.txt"])
    assert code == "QT-USAGE-001" and "attachment notes.txt" in msg
    c, o = commit([work / "shadows.parquet"])
    write_pair(c, o, work.parent / "c.json", work.parent / "o.json")
    for target in ("c.json", "o.json"):
        code, msg, _ = _code(
            certify,
            ["replay"],
            commitments=[(work.parent / "c.json", work.parent / "o.json")],
            out_files=[work.parent / target],
        )
        assert code == "QT-USAGE-001" and "would overwrite the certified" in msg
    assert (work / "shadows.parquet").read_bytes() == before


def test_refuses_service_references(work: Path) -> None:
    shutil.copytree(work / "replay", work / "svc")
    s = json.loads((work / "svc/summary.json").read_text())
    s["spec"]["dataset"] = {"dataset_id": "ds_01J0000000000000000000000"}
    (work / "svc/summary.json").write_text(json.dumps(s))
    code, msg, _ = _code(certify, ["svc"])
    assert code == "QT-INPUT-002" and "service object" in msg


def test_refuses_symlinks_in_runs(work: Path) -> None:
    (work / "replay" / "link.parquet").symlink_to(work / "replay" / "bbo.parquet")
    code, msg, _ = _code(certify, ["replay"])
    assert code == "QT-INPUT-002" and "symbolic link" in msg


def test_verify_refuses_unreadable_certificates(work: Path) -> None:
    assert _code(verify, work / "none.json")[0] == "QT-INPUT-001"
    (work.parent / "x.json").write_text('{"schema": "qt.replay_summary"}')
    assert _code(verify, work.parent / "x.json")[0] == "QT-INPUT-002"
    (work.parent / "y.json").write_text("[1")
    assert _code(verify, work.parent / "y.json")[0] == "QT-INPUT-002"


def test_verify_reports_schema_invalid_certificates(work: Path) -> None:
    doc = make_cert().model_dump(mode="json")
    del doc["runs"]
    ver = verify(write(doc, work.parent / "bad.json"))
    assert ver.status == "fail" and failed(ver) == ["schema"]
    assert ver.certificate.certificate_id is None
