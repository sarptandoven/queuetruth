"""`queuetruth verify atlas` on a synthetic published atlas (invented hashes, no market data): the file hashes, the
atlas root over the day roots, one day's leaves and proofs (from a directory, a tarball or the release, with consent),
and one unit re-run through a stub checkout."""

from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest
from check import synth_atlas as sa

from queuetruth import cli
from queuetruth.atlas import verify as V
from queuetruth.errors import QtError

UNIT = {"schema": "qt.scale.unit", "unit_id": "11092026-b003-u0001",
        "bucket": {"file": "11092026.iex.bucket-03-of-32.pcap.gz"},
        "ingest": {"format": "iex_deep_plus", "venue": "iex"}, "seed": 7}  # fmt: skip


def _leaf(unit: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"unit_id": unit["unit_id"], "unit_spec_sha256": V.sha256_hex(V.canonical_json(unit)), **(extra or {})}


def _day(tmp: Path) -> tuple[Path, dict[str, Any]]:
    """A reduced day directory (`day_manifest.json`, `plan.json`) with three units; returns it and its manifest."""
    units = [UNIT, {**UNIT, "unit_id": "11092026-b004-u0002"}, {**UNIT, "unit_id": "11092026-b005-u0003"}]
    hashes = [V.leaf_hash(_leaf(u)) for u in units]
    plan = (json.dumps({"schema": "qt.scale.plan", "units": units}) + "\n").encode()
    dm = {
        "schema": "qt.scale.day_manifest", "date": "20261109", "merkle_root": V.merkle_root(hashes),
        "source": {"url": "https://example.org/20261109_IEXTP1_DPLS1.0.pcap.gz", "buckets": 32},
        "leaves": [{"unit_id": u["unit_id"], "leaf_hash": h, "proof": V.merkle_proof(hashes, i)}
                   for i, (u, h) in enumerate(zip(units, hashes, strict=True))],
        "plan_sha256": V.sha256_hex(plan), "builds": [{"git_head": "abc123", "units": 3}],
    }  # fmt: skip
    d = tmp / "day-2026-11-09"
    d.mkdir(parents=True)
    (d / "day_manifest.json").write_text(json.dumps(dm))
    (d / "plan.json").write_bytes(plan)
    return d, dm


@pytest.fixture
def day(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    return _day(tmp_path / "days")


@pytest.fixture
def site(tmp_path: Path, day: tuple[Path, dict[str, Any]]) -> Path:
    roots = ["11" * 32, day[1]["merkle_root"], "33" * 32]
    return sa.write_atlas(tmp_path / "site", day_roots=roots)


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Any:
    def _run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        got = capsys.readouterr()
        return code, got.out, got.err

    return _run


def test_merkle_helpers_are_the_pipelines() -> None:
    leaves = [V.sha256_hex(bytes([i])) for i in range(5)]
    root = V.merkle_root(leaves)
    assert all(V.verify_proof(x, V.merkle_proof(leaves, i), root) for i, x in enumerate(leaves))
    assert V.merkle_root([]) == V.sha256_hex(b"") and V.merkle_root(leaves[:1]) == leaves[0]
    with pytest.raises(ValueError):
        V.canonical_json({"x": float("nan")})
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts" / "scale"))
    try:
        import common as K
    except ImportError:  # scripts/realdata helpers missing in an installed-wheel run
        return
    assert K.merkle_root is V.merkle_root and K.leaf_hash is V.leaf_hash and K.canonical_json is V.canonical_json


def test_a_clean_atlas_verifies(site: Path) -> None:
    rep = V.verify_atlas(site)
    assert rep.ok, rep.text()
    names = [c.name for c in rep.checks]
    assert names == ["manifest", "files", "document roots", "atlas root", "days"]
    assert rep.summary.startswith("Verified: all 5 checks")
    assert "recompute" not in rep.summary and "3 day roots" in rep.text()


def test_a_damaged_file_or_root_fails(site: Path) -> None:
    p = site / "atlas" / "v1" / "touch.json"
    p.write_bytes(p.read_bytes().replace(b"standard", b"standarx"))
    rep = V.verify_atlas(site)
    assert not rep.ok and [c.name for c in rep.checks if not c.ok] == ["files"]
    assert "touch.json" in rep.summary and rep.summary.startswith("NOT verified")


def test_atlas_root_must_recompute_from_the_day_roots(site: Path) -> None:
    am_path = site / "data" / "atlas_manifest.json"
    am = json.loads(am_path.read_text())
    am["days"][0]["merkle_root"] = "44" * 32
    am_path.write_text(json.dumps(am))
    rep = V.verify_atlas(site)
    assert [c.name for c in rep.checks if not c.ok] == ["atlas root"]


def test_atlas_manifest_missing_or_given(site: Path, tmp_path: Path) -> None:
    am = site / "data" / "atlas_manifest.json"
    kept = tmp_path / "atlas_manifest.json"
    kept.write_bytes(am.read_bytes())
    am.unlink()
    rep = V.verify_atlas(site)
    assert not rep.ok and any("--atlas-manifest" in n for n in rep.notes)
    assert V.verify_atlas(site / "atlas" / "v1", atlas_manifest=kept).ok


def test_one_day_from_a_directory_and_a_tarball(site: Path, day: tuple[Path, dict[str, Any]], tmp_path: Path) -> None:
    rep = V.verify_atlas(site, day=day[0])
    assert rep.ok, rep.text()
    assert [c.name for c in rep.checks][-4:] == ["day root listed", "day root", "leaf proofs", "plan"]
    tar = tmp_path / "day.tar.gz"
    with tarfile.open(tar, "w:gz") as tf:
        for n in ("day_manifest.json", "plan.json"):
            tf.add(day[0] / n, arcname=f"./{n}")
    assert V.verify_atlas(site, day=tar).ok
    rep = V.verify_atlas(site, day=day[0], unit=UNIT["unit_id"])
    assert rep.ok and rep.checks[-1].name == "unit leaf" and any("--rerun" in n for n in rep.notes)
    with pytest.raises(QtError, match="no unit nope"):
        V.verify_atlas(site, day=day[0], unit="nope")


def test_a_day_the_atlas_does_not_list_fails(tmp_path: Path, day: tuple[Path, dict[str, Any]]) -> None:
    other = sa.write_atlas(tmp_path / "other")  # invented day roots: not this day's
    rep = V.verify_atlas(other, day=day[0])
    assert [c.name for c in rep.checks if not c.ok] == ["day root listed"]


def test_a_damaged_leaf_fails(site: Path, day: tuple[Path, dict[str, Any]]) -> None:
    dm = dict(day[1])
    dm["leaves"] = [dict(x) for x in dm["leaves"]]
    dm["leaves"][1]["leaf_hash"] = "55" * 32
    (day[0] / "day_manifest.json").write_text(json.dumps(dm))
    rep = V.verify_atlas(site, day=day[0])
    assert {c.name for c in rep.checks if not c.ok} == {"day root", "leaf proofs"}


def test_day_by_date_downloads_from_the_release_with_consent(
    site: Path, day: tuple[Path, dict[str, Any]], tmp_path: Path, monkeypatch: Any
) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for n in ("day_manifest.json", "plan.json"):
            tf.add(day[0] / n, arcname=n)
    asked: list[str] = []

    def fetch(url: str) -> bytes:
        asked.append(url)
        assert url == "https://github.com/sarptandoven/queuetruth/releases/download/atlas-days/day-2026-11-09.tar.gz"
        return buf.getvalue()

    with pytest.raises(QtError, match="nothing was downloaded"):
        V.verify_atlas(site, day="2026-11-09", fetch=fetch, confirm=lambda url: False)
    assert asked == []
    assert V.verify_atlas(site, day="2026-11-09", fetch=fetch, yes=True).ok
    with pytest.raises(QtError, match="no day 2026-11-11"):
        V.verify_atlas(site, day="2026-11-11", fetch=fetch, yes=True)


def test_remote_atlas_through_the_cache(site: Path, day: tuple[Path, dict[str, Any]], tmp_path: Path,
                                        monkeypatch: Any) -> None:  # fmt: skip
    monkeypatch.setenv("QT_CACHE_DIR", str(tmp_path / "cache"))
    calls: list[str] = []
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for n in ("day_manifest.json", "plan.json"):
            tf.add(day[0] / n, arcname=n)

    def fetch(url: str) -> bytes:
        calls.append(url)
        if url.endswith(".tar.gz"):
            return buf.getvalue()
        return (site / url.removeprefix("https://example.org/")).read_bytes()

    url = "https://example.org/atlas/v1/"
    with pytest.raises(QtError, match="nothing was downloaded"):
        V.verify_atlas(url, fetch=fetch)
    assert calls == []
    rep = V.verify_atlas(url, fetch=fetch, yes=True, day="2026-11-09")
    assert rep.ok, rep.text()
    assert "https://example.org/data/atlas_manifest.json" in calls
    n = len(calls)
    assert V.verify_atlas(url, fetch=fetch, day="2026-11-09").ok  # offline from the cache, nothing asked
    assert len(calls) == n


def _checkout(tmp: Path, unit_out: dict[str, Any] | None) -> Path:
    """A stub checkout: `unit.py` writes a shard manifest with the leaf of the unit (or `unit_out`), `iex_split.py`
    writes the requested bucket."""
    s = tmp / "co" / "scripts" / "scale"
    s.mkdir(parents=True)
    (s / "common.py").write_text("")
    pkg = str(Path(__file__).resolve().parents[2])
    (s / "unit.py").write_text(
        "import argparse, json, pathlib, sys\n"
        f"sys.path.insert(0, {pkg!r})\n"
        "from queuetruth.atlas.verify import leaf_hash, sha256_hex, canonical_json\n"
        "ap = argparse.ArgumentParser()\n"
        "for a in ('--unit', '--unit-id', '--split', '--out', '--threads'): ap.add_argument(a)\n"
        "a = ap.parse_args()\n"
        "plan = json.loads(pathlib.Path(a.unit).read_text())\n"
        "u = next(x for x in plan['units'] if x['unit_id'] == a.unit_id)\n"
        "assert (pathlib.Path(a.split) / u['bucket']['file']).is_file(), 'no bucket'\n"
        f"leaf = {unit_out!r} or "
        "{'unit_id': u['unit_id'], 'unit_spec_sha256': sha256_hex(canonical_json(u))}\n"
        "out = pathlib.Path(a.out); out.mkdir(parents=True)\n"
        "(out / 'shard_manifest.json').write_text(json.dumps({'leaf': leaf, 'leaf_hash': leaf_hash(leaf)}))\n"
    )
    (s / "iex_split.py").write_text(
        "import argparse, pathlib\n"
        "ap = argparse.ArgumentParser()\n"
        "for a in ('--url', '--date', '--buckets', '--only-bucket', '--out', '--threads'): ap.add_argument(a)\n"
        "a = ap.parse_args()\n"
        "d = pathlib.Path(a.out) / '11092026'; d.mkdir(parents=True)\n"
        "(d / f'11092026.iex.bucket-{int(a.only_bucket):02d}-of-{int(a.buckets):02d}.pcap.gz').write_bytes(b'x')\n"
    )
    return tmp / "co"


def test_rerun_a_unit_matches_its_leaf(site: Path, day: tuple[Path, dict[str, Any]], tmp_path: Path) -> None:
    co = _checkout(tmp_path, None)
    split = tmp_path / "split"
    split.mkdir()
    (split / UNIT["bucket"]["file"]).write_bytes(b"x")
    rep = V.verify_atlas(site, day=day[0], unit=UNIT["unit_id"], rerun=True, split=split, checkout=co)
    assert rep.ok, rep.text()
    assert [c.name for c in rep.checks][-2:] == ["unit spec", "unit re-run"]
    assert any("this checkout is" in n for n in rep.notes)
    # streamed: the stub split keeps only the unit's bucket; asks first
    with pytest.raises(QtError, match="about 9 GB"):
        V.verify_atlas(site, day=day[0], unit=UNIT["unit_id"], rerun=True, checkout=co, confirm=lambda u: False)
    rep = V.verify_atlas(site, day=day[0], unit=UNIT["unit_id"], rerun=True, checkout=co, yes=True)
    assert rep.ok, rep.text()


def test_rerun_a_unit_that_differs_fails(site: Path, day: tuple[Path, dict[str, Any]], tmp_path: Path) -> None:
    co = _checkout(tmp_path, {"unit_id": UNIT["unit_id"], "unit_spec_sha256": "00" * 32})
    split = tmp_path / "split"
    split.mkdir()
    (split / UNIT["bucket"]["file"]).write_bytes(b"x")
    rep = V.verify_atlas(site, day=day[0], unit=UNIT["unit_id"], rerun=True, split=split, checkout=co)
    assert {c.name for c in rep.checks if not c.ok} == {"unit spec", "unit re-run"}


def test_rerun_needs_a_checkout(site: Path, day: tuple[Path, dict[str, Any]], tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(QtError, match="needs a queuetruth checkout"):
        V.verify_atlas(site, day=day[0], unit=UNIT["unit_id"], rerun=True, checkout=tmp_path)
    with pytest.raises(QtError, match="--unit needs --day"):
        V.verify_atlas(site, unit=UNIT["unit_id"])


def test_cli(run: Any, site: Path, day: tuple[Path, dict[str, Any]]) -> None:
    code, out, err = run("verify", "atlas", "--from", str(site), "--day", str(day[0]))
    assert code == 0, err
    assert out.startswith("Verified: all 9 checks") and "ok    day root:" in out
    code, out, _ = run("verify", "atlas", "--from", str(site), "--json")
    doc = json.loads(out)
    assert code == 0 and doc["schema"] == "qt.atlas_verify" and doc["ok"] is True
    (site / "atlas" / "v1" / "summary.json").write_text("{}")
    code, out, _ = run("verify", "atlas", "--from", str(site))
    assert code == 1 and out.startswith("NOT verified") and "FAIL  files" in out
    code, out, _ = run("help", "verify", "atlas")
    assert code == 0 and out.startswith("usage: queuetruth verify atlas")
    code, out, _ = run("--help")
    assert any(line.split()[:1] == ["verify"] for line in out.splitlines())


def test_cli_asks_and_names_each_download(run: Any, tmp_path: Path, monkeypatch: Any) -> None:
    from queuetruth.atlas import _cli

    monkeypatch.setenv("QT_CACHE_DIR", str(tmp_path / "cache"))
    code, out, err = run("verify", "atlas", "--from", "https://example.org/atlas/v1/")
    assert code == 2 and out == "" and "the atlas data API at https://example.org/atlas/v1/" in err
    confirm = _cli._verify_confirm(interactive=False)
    with pytest.raises(QtError, match="about 9 GB"):
        confirm("https://example.org/20261109_IEXTP1_DPLS1.0.pcap.gz")
    with pytest.raises(QtError, match="the reduced day"):
        confirm("https://example.org/day-2026-11-09.tar.gz")
