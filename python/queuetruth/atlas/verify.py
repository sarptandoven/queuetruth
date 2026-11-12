"""Verify a published Fill-Realism Atlas without a checkout: `queuetruth verify atlas`.

What is checked, in order (each check is a line of the report, with what it compared):

1. **The data API.** `atlas/v1/manifest.json` is read (or downloaded, with consent) and every file it lists is checked
   against its sha256 and its document model; every document names the manifest's atlas root.
2. **The atlas root.** The site's `data/atlas_manifest.json` (next to `atlas/v1/`, or `atlas_manifest=FILE`) lists
   every day's root. Its atlas root must be the API's, and must recompute from the day roots in date order
   (`merkle_root`); its days must be the API's days.
3. **One day** (`day=`): the reduced day (`day-<date>.tar.gz` on the repository's `atlas-days` release, downloaded
   with consent, or a local directory or tarball) holds `day_manifest.json` and `plan.json`. The atlas must list this
   day's root, the day root must recompute from its leaves, every leaf's Merkle proof must reach it, and the plan
   must hash to the day's `plan_sha256`.
4. **One unit** (`unit=`, with `day=`): the unit is in the plan and the day's leaves. With `rerun=True` it is run
   again as `scripts/scale/verify_unit.py` does: the unit runner (`scripts/scale/unit.py` of a checkout at the
   commit the day was built at, since a leaf holds sha256 digests of Parquet files that build wrote) runs on the
   unit's bucket (`split=DIR`, or streamed from the day's public IEX file with `scripts/scale/iex_split.py
   --only-bucket`, with consent: about 9 GB is read, one bucket is kept), and its leaf hash is compared with the
   published one.

The hashing rules (shared with the pipeline, `scripts/scale/common.py`): `canonical_json` is sorted-key compact
JSON as UTF-8 with no NaN; a leaf is `sha256(0x00 + canonical_json(leaf))`; an inner node is
`sha256(0x01 + left + right)`; an odd node is carried up unchanged; the root of no leaves is `sha256("")`.

Standard library only; the engine is never imported.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError

#: Where the reduced days are kept: the release tag and the file prefix of each venue.
REPO_URL = "https://github.com/sarptandoven/queuetruth"
DAY_RELEASES = {"iex": ("atlas-days", "day-"), "nasdaq": ("atlas-days-nasdaq", "nasdaq-day-")}
#: Largest day file downloaded (a reduced day is tens of MB).
MAX_DAY_BYTES = 1 << 30

# ---- hashing and the Merkle tree (the pipeline's rules; scripts/scale/common.py re-exports these) ----------------


def canonical_json(obj: Any) -> bytes:
    """Sorted-key compact JSON as UTF-8; NaN and infinities are refused."""

    def check(x: Any) -> None:
        if isinstance(x, float) and not math.isfinite(x):
            raise ValueError("canonical_json: NaN / inf are not allowed")
        if isinstance(x, dict):
            for v in x.values():
                check(v)
        elif isinstance(x, list | tuple):
            for v in x:
                check(v)

    check(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def leaf_hash(leaf_doc: dict[str, Any]) -> str:
    return sha256_hex(b"\x00" + canonical_json(leaf_doc))


def _level_up(level: list[bytes]) -> list[bytes]:
    nxt = []
    for i in range(0, len(level), 2):
        if i + 1 < len(level):
            nxt.append(hashlib.sha256(b"\x01" + level[i] + level[i + 1]).digest())
        else:
            nxt.append(level[i])
    return nxt


def merkle_root(leaves: Sequence[str]) -> str:
    """The root over hex leaf hashes, in the order given."""
    if not leaves:
        return sha256_hex(b"")
    level = [bytes.fromhex(x) for x in leaves]
    while len(level) > 1:
        level = _level_up(level)
    return level[0].hex()


def merkle_proof(leaves: Sequence[str], index: int) -> list[list[str]]:
    """Sibling path of leaf `index`: `[["L" or "R", sibling hex], ...]` from the leaf up (carried nodes skipped)."""
    level = [bytes.fromhex(x) for x in leaves]
    path: list[list[str]] = []
    i = index
    while len(level) > 1:
        sib = i ^ 1
        if sib < len(level):
            path.append(["L" if sib < i else "R", level[sib].hex()])
        level = _level_up(level)
        i //= 2
    return path


def verify_proof(leaf: str, proof: Sequence[Sequence[str]], root: str) -> bool:
    """Whether `proof` takes `leaf` to `root`."""
    h = bytes.fromhex(leaf)
    for side, sib in proof:
        s = bytes.fromhex(sib)
        h = hashlib.sha256(b"\x01" + (s + h if side == "L" else h + s)).digest()
    return h.hex() == root


def diff_fields(a: object, b: object, path: str = "") -> list[str]:
    """The dotted paths where two JSON documents differ."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += diff_fields(a.get(k), b.get(k), f"{path}.{k}" if path else k)
        return out
    return [] if a == b else [path]


def check_build(day: dict[str, Any], here: dict[str, Any]) -> list[str]:
    """Lines saying whether this checkout is the build the day's units ran with (`day_manifest.json` `builds`)."""
    builds = day.get("builds") or []
    if not builds:
        return ["the day manifest names no build (written before builds were recorded)"]
    heads = [b.get("git_head") for b in builds]
    if here.get("git_head") in heads:
        return [f"this checkout is {here.get('git_head')}, a build the day ran with"]
    return [
        f"this checkout is {here.get('git_head') or 'not a git checkout'}; the day ran with "
        + ", ".join(f"{h} ({b.get('units')} units)" for h, b in zip(heads, builds, strict=True)),
        "a leaf holds sha256 digests of Parquet files the build wrote, so another build may not reproduce it: "
        f"git checkout {heads[0]} && make develop",
    ]


def check_atlas(day: dict[str, Any], atlas: dict[str, Any]) -> tuple[bool, str]:
    """Whether the atlas lists this day's root for its date and its root recomputes from the listed day roots."""
    listed = [d for d in atlas.get("days", []) if d.get("date") == day.get("date")]
    if not listed:
        return False, f"the atlas manifest lists no day {day.get('date')}"
    if listed[0].get("merkle_root") != day.get("merkle_root"):
        listed_root, day_root = listed[0].get("merkle_root"), day.get("merkle_root")
        return False, f"the atlas lists day root {listed_root}, the day manifest has {day_root}"
    root = merkle_root([d["merkle_root"] for d in atlas["days"]])
    if root != atlas.get("atlas_root"):
        return False, f"the atlas root does not recompute from its day roots ({root} != {atlas.get('atlas_root')})"
    return True, f"the atlas lists this day root, and its root {root} recomputes from the day roots"


# ---- the report -----------------------------------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Report:
    """What was checked: `checks` (each with `ok`), `.ok` when all passed, `.summary` in plain words."""

    source: str
    atlas_root: str | None = None
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str) -> bool:
        self.checks.append(Check(name, bool(ok), detail))
        return bool(ok)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    @property
    def summary(self) -> str:
        n, bad = len(self.checks), [c for c in self.checks if not c.ok]
        if not bad:
            return f"Verified: all {n} checks of the atlas at {self.source} passed (atlas root {self.atlas_root})."
        return (
            f"NOT verified: {len(bad)} of {n} checks of the atlas at {self.source} failed: "
            + "; ".join(f"{c.name} ({c.detail})" for c in bad[:3])
            + ("; ..." if len(bad) > 3 else "")
            + "."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "qt.atlas_verify",
            "schema_version": 1,
            "summary": self.summary,
            "ok": self.ok,
            "source": self.source,
            "atlas_root": self.atlas_root,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
            "notes": list(self.notes),
        }

    def text(self) -> str:
        out = [self.summary, ""]
        out += [f"{'ok  ' if c.ok else 'FAIL'}  {c.name}: {c.detail}" for c in self.checks]
        out += [f"note: {n}" for n in self.notes]
        return "\n".join(out)


# ---- downloads -------------------------------------------------------------------------------------------------------


def _get_large(url: str, limit: int = MAX_DAY_BYTES, timeout_s: float = 60.0) -> bytes:
    """One HTTPS GET of at most `limit` bytes, read in chunks (`QT-INPUT-001` on a network error)."""
    from queuetruth._version import __version__

    req = urllib.request.Request(url, headers={"User-Agent": f"queuetruth/{__version__} (atlas verify)"})
    buf = io.BytesIO()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:  # https only, checked by the caller
            while chunk := r.read(1 << 20):
                buf.write(chunk)
                if buf.tell() > limit:
                    raise QtError("QT-INPUT-002", f"verify: {url} is larger than {limit >> 20} MB")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise QtError("QT-INPUT-001", f"verify: could not download {url}: {e}", "check the connection") from e
    return buf.getvalue()


def _site_file(src: Any, rel: str, *, fetch: Callable[[str], bytes] | None) -> tuple[bytes | None, str]:
    """A file of the site next to the API (`../../data/atlas_manifest.json`): (bytes or None, where)."""
    from queuetruth.atlas._lookup import LocalSource, RemoteSource

    if isinstance(src, LocalSource):
        p = (src.base / rel).resolve()
        return (p.read_bytes() if p.is_file() else None), str(p)
    assert isinstance(src, RemoteSource)
    url = urllib.parse.urljoin(src.url, rel)
    cached = src.cache / "_site" / Path(urllib.parse.urlsplit(url).path).name
    if cached.is_file() and src.offline:
        return cached.read_bytes(), f"{url} (cached)"
    if src._asked is None:
        src._asked = bool(src._consent(url))
    if not src._asked:
        raise QtError("QT-USAGE-001", f"verify: needs {url} and nothing was downloaded", "rerun with --yes")
    try:
        data = (fetch or src._fetch)(url)
    except QtError:
        return None, url
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(data)
    return data, url


def _iso(d: str) -> str:
    s = str(d)
    return f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 and s.isdigit() else s


# ---- 1 and 2: the API and the atlas root -----------------------------------------------------------------------------


def verify_api(
    source: str | Path | None = None,
    *,
    atlas_manifest: str | Path | None = None,
    yes: bool = False,
    confirm: Callable[[str], bool] | None = None,
    refresh: bool = False,
    fetch: Callable[[str], bytes] | None = None,
) -> tuple[Report, Any, dict[str, Any] | None]:
    """Checks 1 and 2 of the module docstring; returns (report, the API source, the atlas manifest or None)."""
    from queuetruth.atlas._lookup import _load, _validate, open_source

    src = open_source(source, yes=yes, confirm=confirm, refresh=refresh, fetch=fetch)
    m = src.manifest
    rep = Report(source=src.where, atlas_root=m["atlas_root"])
    rep.add("manifest", True, f"manifest.json is a valid qt.atlas_api_manifest listing {len(m['files'])} files")
    bad: list[str] = []
    roots: list[str] = []
    for f in m["files"]:
        try:
            data = src._raw(f["path"])
        except QtError as e:
            bad.append(f"{f['path']}: {e.message}")
            continue
        if sha256_hex(data) != f["sha256"] or len(data) != int(f["bytes"]):
            bad.append(f"{f['path']}: sha256 or size differs from the manifest")
            continue
        try:
            doc = _validate(_load(data, f["path"]), f["path"])
        except QtError as e:
            bad.append(f"{f['path']}: {e.message}")
            continue
        if "atlas_root" in doc and doc["atlas_root"] != m["atlas_root"]:
            roots.append(f["path"])
    rep.add(
        "files",
        not bad,
        f"every file's sha256, size and model match the manifest ({len(m['files'])} files)"
        if not bad
        else "; ".join(bad[:5]) + ("; ..." if len(bad) > 5 else ""),
    )
    rep.add(
        "document roots",
        not roots,
        "every document names the manifest's atlas root" if not roots else f"another atlas root in {', '.join(roots)}",
    )
    if atlas_manifest is not None:
        p = Path(atlas_manifest).expanduser()
        if not p.is_file():
            raise QtError("QT-INPUT-001", f"verify: {p}: no such file")
        raw, where = p.read_bytes(), str(p)
    else:
        raw, where = _site_file(src, "../../data/atlas_manifest.json", fetch=fetch)
    if raw is None:
        rep.add(
            "atlas root",
            False,
            f"no atlas manifest with the day roots at {where}",
        )
        rep.notes.append("pass --atlas-manifest FILE (the site's data/atlas_manifest.json) to check the day roots")
        return rep, src, None
    try:
        am = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        rep.add("atlas root", False, f"{where} is not JSON: {e}")
        return rep, src, None
    days = am.get("days") or []
    day_roots = [str(d.get("merkle_root")) for d in days]
    root = merkle_root(day_roots) if all(re.fullmatch(r"[0-9a-f]{64}", r) for r in day_roots) else None
    rep.add(
        "atlas root",
        root is not None and root == am.get("atlas_root") == m["atlas_root"],
        f"the root over the {len(days)} day roots of {where} is {root}; the atlas manifest says "
        f"{am.get('atlas_root')}, the API {m['atlas_root']}",
    )
    dates = [_iso(d.get("date")) for d in days]
    rep.add(
        "days",
        dates == list(m["days"]) and dates == sorted(dates),
        f"the atlas manifest lists the API's {len(m['days'])} days in date order"
        if dates == list(m["days"]) and dates == sorted(dates)
        else f"the atlas manifest lists {', '.join(dates[:5])}..., the API {', '.join(m['days'][:5])}...",
    )
    return rep, src, am


# ---- 3: one day ----------------------------------------------------------------------------------------------------


def _day_entry(am: dict[str, Any], day: str) -> dict[str, Any]:
    key = day.replace("-", "")
    hit = [d for d in am.get("days") or [] if str(d.get("date")) == key]
    if not hit:
        shown = ", ".join(_iso(d.get("date")) for d in (am.get("days") or [])[:30])
        raise QtError("QT-USAGE-001", f"verify: the atlas has no day {day}", f"its days: {shown}")
    return hit[0]


def day_url(entry: dict[str, Any]) -> str:
    """The URL of the reduced day file on the repository's release."""
    release, prefix = DAY_RELEASES.get(str(entry.get("venue") or "iex"), DAY_RELEASES["iex"])
    return f"{REPO_URL}/releases/download/{release}/{prefix}{_iso(entry['date'])}.tar.gz"


def _read_day_tar(data: bytes) -> dict[str, bytes]:
    """`day_manifest.json` and `plan.json` from a day tarball (nothing is extracted to disk)."""
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        for member in tf:
            name = Path(member.name).name
            if member.isfile() and name in ("day_manifest.json", "plan.json") and name not in out:
                fh = tf.extractfile(member)
                if fh is not None:
                    out[name] = fh.read()
    return out


def load_day(
    day: str | Path,
    am: dict[str, Any] | None,
    *,
    consent: Callable[[str], bool],
    cache: Path | None,
    fetch: Callable[[str], bytes] | None = None,
) -> tuple[dict[str, Any], bytes, str]:
    """(day manifest, plan bytes, where) of `day`: a directory or tarball, or a date of the atlas (downloaded from
    the release with consent, then kept in `cache`)."""
    p = Path(str(day)).expanduser()
    files: dict[str, bytes]
    if p.is_dir():
        files = {n: (p / n).read_bytes() for n in ("day_manifest.json", "plan.json") if (p / n).is_file()}
        where = str(p)
    elif p.is_file():
        files, where = _read_day_tar(p.read_bytes()), str(p)
    else:
        if not re.fullmatch(r"\d{4}-?\d{2}-?\d{2}", str(day)):
            raise QtError("QT-USAGE-001", f"verify: --day {day} is not a date, a day directory or a day file")
        if am is None:
            raise QtError("QT-INPUT-001", "verify: --day DATE needs the atlas manifest (its day roots and venue)")
        entry = _day_entry(am, str(day))
        url = day_url(entry)
        here = cache / Path(urllib.parse.urlsplit(url).path).name.removesuffix(".tar.gz") if cache else None
        if here is not None and (here / "day_manifest.json").is_file() and (here / "plan.json").is_file():
            files = {n: (here / n).read_bytes() for n in ("day_manifest.json", "plan.json")}
            where = f"{url} (cached in {here})"
        else:
            if not consent(url):
                raise QtError(
                    "QT-USAGE-001",
                    f"verify: the day needs {url} and nothing was downloaded",
                    "rerun with --yes, or pass --day DIR with the unpacked day",
                )
            files, where = _read_day_tar((fetch or _get_large)(url)), url
            if here is not None:
                here.mkdir(parents=True, exist_ok=True)
                for n, b in files.items():
                    (here / n).write_bytes(b)
    if "day_manifest.json" not in files or "plan.json" not in files:
        raise QtError("QT-INPUT-002", f"verify: {where} has no day_manifest.json and plan.json")
    return json.loads(files["day_manifest.json"]), files["plan.json"], where


def verify_day(rep: Report, dm: dict[str, Any], plan: bytes, am: dict[str, Any] | None, where: str) -> None:
    """Check 3 of the module docstring, added to `rep`."""
    if am is not None:
        ok, msg = check_atlas(dm, am)
        rep.add("day root listed", ok, msg)
    leaves = [x["leaf_hash"] for x in dm.get("leaves") or []]
    root = merkle_root(leaves)
    rep.add(
        "day root",
        root == dm.get("merkle_root"),
        f"the root over the {len(leaves)} leaves of {where} is {root}; the day manifest says {dm.get('merkle_root')}",
    )
    bad = [x["unit_id"] for x in dm.get("leaves") or [] if not verify_proof(x["leaf_hash"], x["proof"], root)]
    rep.add(
        "leaf proofs",
        not bad,
        f"all {len(leaves)} leaf proofs reach the day root" if not bad else f"proofs fail for {', '.join(bad[:5])}",
    )
    if dm.get("plan_sha256"):
        rep.add(
            "plan",
            sha256_hex(plan) == dm["plan_sha256"],
            "plan.json hashes to the day manifest's plan_sha256",
        )


# ---- 4: one unit ---------------------------------------------------------------------------------------------------


def unit_of(dm: dict[str, Any], plan: dict[str, Any], unit_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(unit spec, leaf entry) of `unit_id` (`QT-USAGE-001` when it is in neither)."""
    unit = next((u for u in plan.get("units") or [] if u["unit_id"] == unit_id), None)
    entry = next((x for x in dm.get("leaves") or [] if x["unit_id"] == unit_id), None)
    if unit is None or entry is None:
        some = ", ".join(x["unit_id"] for x in (dm.get("leaves") or [])[:5])
        raise QtError("QT-USAGE-001", f"verify: no unit {unit_id} in the day's plan and leaves", f"units: {some}, ...")
    return unit, entry


def find_checkout(given: str | Path | None) -> Path | None:
    """A queuetruth checkout with the unit runner: `given`, else the current directory or one of its parents."""
    cands = [Path(given).expanduser()] if given else [Path.cwd(), *Path.cwd().parents]
    for c in cands:
        if (c / "scripts" / "scale" / "unit.py").is_file() and (c / "scripts" / "scale" / "common.py").is_file():
            return c.resolve()
    return None


def git_head(checkout: Path) -> str | None:
    try:
        cmd = ["git", "-C", str(checkout), "rev-parse", "HEAD"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def rerun_unit(
    rep: Report,
    dm: dict[str, Any],
    plan_bytes: bytes,
    unit_id: str,
    *,
    checkout: Path,
    split: Path | None,
    consent: Callable[[str], bool],
    threads: int = 1,
    work: Path | None = None,
    python: str | None = None,
) -> None:
    """Check 4 with `rerun`: runs the unit with the checkout's runner and compares its leaf (added to `rep`)."""
    plan = json.loads(plan_bytes)
    unit, entry = unit_of(dm, plan, unit_id)
    for line in check_build(dm, {"git_head": git_head(checkout)}):
        rep.notes.append(line)
    py = python or sys.executable
    with tempfile.TemporaryDirectory(prefix="qt-verify-", dir=work) as tmp:
        t = Path(tmp)
        (t / "plan.json").write_bytes(plan_bytes)
        if split is None:
            split = _stream_bucket(unit, dm, t, checkout=checkout, consent=consent, python=py, threads=threads)
        r = subprocess.run(
            [py, str(checkout / "scripts" / "scale" / "unit.py"), "--unit", str(t / "plan.json"), "--unit-id",
             unit_id, "--split", str(split), "--out", str(t / "shard"), "--threads", str(threads)],
            capture_output=True, text=True,
        )  # fmt: skip
        mp = t / "shard" / "shard_manifest.json"
        if r.returncode != 0 or not mp.is_file():
            tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
            rep.add("unit re-run", False, f"the unit runner failed (exit {r.returncode}): {' '.join(tail)}")
            return
        m = json.loads(mp.read_text())
    same = m["leaf_hash"] == entry["leaf_hash"]
    how = f"re-run leaf {m['leaf_hash']} {'==' if same else '!='} published {entry['leaf_hash']}"
    if not same and "exactness_by_symbol" in m["leaf"]:
        old = leaf_hash({k: v for k, v in m["leaf"].items() if k != "exactness_by_symbol"})
        if old == entry["leaf_hash"]:
            same, how = True, f"re-run leaf without exactness_by_symbol {old} == published (pre-attribution leaf)"
    spec_ok = m["leaf"].get("unit_spec_sha256") == sha256_hex(canonical_json(unit))
    rep.add("unit spec", spec_ok, "the unit spec in the plan hashes to the re-run leaf's unit_spec_sha256")
    rep.add("unit re-run", same, how)


def _stream_bucket(
    unit: dict[str, Any],
    dm: dict[str, Any],
    tmp: Path,
    *,
    checkout: Path,
    consent: Callable[[str], bool],
    python: str,
    threads: int,
) -> Path:
    """Streams the day's public IEX file once and keeps only the unit's bucket; returns the split directory."""
    src = dm.get("source") or {}
    url = src.get("url")
    fmt = (unit.get("ingest") or {}).get("format")
    name = str(unit["bucket"]["file"])
    mt = re.search(r"bucket-(\d+)-of-(\d+)", name)
    if fmt != "iex_deep_plus" or not url or mt is None:
        raise QtError(
            "QT-USAGE-001",
            f"verify: streaming the bucket works for IEX days; this unit's bucket is {name}",
            "re-create the buckets (the site's verify section has the command) and pass --split DIR",
        )
    if not consent(url):
        raise QtError(
            "QT-USAGE-001",
            f"verify: re-running the unit streams {url} (about 9 GB, never stored) and nothing was downloaded",
            "rerun with --yes, or pass --split DIR with the unit's bucket",
        )
    out = tmp / "split"
    r = subprocess.run(
        [python, str(checkout / "scripts" / "scale" / "iex_split.py"), "--url", url, "--date", _iso(dm["date"]),
         "--buckets", str(int(mt.group(2))), "--only-bucket", str(int(mt.group(1))), "--out", str(out),
         "--threads", str(max(threads, 1))],
        capture_output=True, text=True,
    )  # fmt: skip
    if r.returncode != 0:
        tail = " ".join((r.stderr or r.stdout).strip().splitlines()[-3:])
        raise QtError("QT-INPUT-001", f"verify: streaming the bucket failed (exit {r.returncode}): {tail}")
    dirs = [d for d in out.iterdir() if d.is_dir()]
    if len(dirs) != 1 or not (dirs[0] / name).is_file():
        raise QtError("QT-INPUT-002", f"verify: the split did not write {name}")
    return dirs[0]


# ---- all of it -------------------------------------------------------------------------------------------------------


def verify_atlas(
    source: str | Path | None = None,
    *,
    day: str | Path | None = None,
    unit: str | None = None,
    rerun: bool = False,
    split: str | Path | None = None,
    checkout: str | Path | None = None,
    atlas_manifest: str | Path | None = None,
    yes: bool = False,
    confirm: Callable[[str], bool] | None = None,
    refresh: bool = False,
    fetch: Callable[[str], bytes] | None = None,
    threads: int = 1,
) -> Report:
    """Verifies the published atlas (module docstring); `.ok` says whether every check passed. Nothing is downloaded
    unless `yes=True` or `confirm(url)` says yes."""
    if unit is not None and day is None:
        raise QtError("QT-USAGE-001", "verify: --unit needs --day (the day the unit belongs to)")
    if (rerun or split is not None) and unit is None:
        raise QtError("QT-USAGE-001", "verify: --rerun and --split need --day and --unit")
    rep, src, am = verify_api(
        source, atlas_manifest=atlas_manifest, yes=yes, confirm=confirm, refresh=refresh, fetch=fetch
    )
    if day is None:
        return rep

    def consent(url: str) -> bool:
        return True if yes else bool(confirm(url)) if confirm is not None else False

    cache = getattr(src, "cache", None)
    dm, plan, where = load_day(day, am, consent=consent, cache=cache / "_days" if cache else None, fetch=fetch)
    verify_day(rep, dm, plan, am, where)
    if unit is None:
        return rep
    _, entry = unit_of(dm, json.loads(plan), unit)
    rep.add("unit leaf", verify_proof(entry["leaf_hash"], entry["proof"], dm["merkle_root"]),
            f"unit {unit} is in the plan, and its leaf {entry['leaf_hash']} reaches the day root")  # fmt: skip
    if not rerun:
        rep.notes.append(
            "the unit was not re-run (--rerun re-runs it: it needs a checkout at the day's build and the unit's "
            "bucket, streamed from the public day file or given with --split)"
        )
        return rep
    co = find_checkout(checkout)
    if co is None:
        raise QtError(
            "QT-USAGE-001",
            "verify: re-running a unit needs a queuetruth checkout with scripts/scale/unit.py (a leaf holds sha256 "
            "digests of Parquet files the day's build wrote, so the run must use that commit)",
            f"git clone {REPO_URL} && git checkout <the day's git_head> && make develop, then --checkout DIR",
        )
    rerun_unit(
        rep,
        dm,
        plan,
        unit,
        checkout=co,
        split=Path(split) if split is not None else None,
        consent=consent,
        threads=threads,
    )
    return rep


__all__ = [
    "DAY_RELEASES",
    "Check",
    "Report",
    "canonical_json",
    "check_atlas",
    "check_build",
    "day_url",
    "diff_fields",
    "find_checkout",
    "leaf_hash",
    "load_day",
    "merkle_proof",
    "merkle_root",
    "rerun_unit",
    "sha256_hex",
    "unit_of",
    "verify_api",
    "verify_atlas",
    "verify_day",
    "verify_proof",
]
