"""Content checks for queuetruth release artifacts (abi3 wheels and the sdist), run before anything is published.

    python -I scripts/release/check_dist.py DIST [DIST ...] [--source ROOT] [--require-sdist]
        [--require-platform GLOB ...] [--json]

`DIST` is a wheel, an sdist or a directory holding them. With `--source ROOT` (the repository checkout) the
artifacts are also compared with the tree: the version in `pyproject.toml`, `Cargo.toml` and `_version.py`, every
Python file and licence file byte for byte, and every `schemas/json/*.schema.json` embedded verbatim in the
extension module (`crates/cli/build.rs` embeds them with `include_str!`; a build that cannot see the directory
silently embeds none). Stdlib only, so it runs on any CPython 3.11+ without the project installed.

Exit codes: 0 every check passed, 1 at least one check failed, 2 usage error (no artifact, unreadable file).
"""

from __future__ import annotations

import argparse
import base64
import configparser
import fnmatch
import hashlib
import json
import re
import sys
import tarfile
import tomllib
import zipfile
from dataclasses import asdict, dataclass, field
from email.message import Message
from email.parser import HeaderParser
from pathlib import Path, PurePosixPath

NAME = "queuetruth"
LICENSE_EXPRESSION = "Apache-2.0"
REQUIRES_PYTHON = ">=3.11"
EXTRAS = {"client", "mcp", "polars"}
BASE_DEPS = {"pyarrow", "numpy", "scipy", "scikit-learn", "pydantic"}
LICENSE_FILES = [
    "LICENSE",
    "NOTICE",
    "third_party/licenses/hftbacktest-MIT.txt",
    "third_party/licenses/rust-crates.txt",
]
CONSOLE_SCRIPT = ("queuetruth", "queuetruth.cli:main")
PY_TAG, ABI_TAG = "cp311", "abi3"

#: Package files every wheel must carry (the extension module is checked separately).
WHEEL_REQUIRED = [
    "queuetruth/__init__.py",
    "queuetruth/_version.py",
    "queuetruth/_core.pyi",
    "queuetruth/py.typed",
    "queuetruth/api.py",
    "queuetruth/cli.py",
    "queuetruth/errors.py",
    "queuetruth/client/__init__.py",
    "queuetruth/mcp/server.py",
    "queuetruth/report/__init__.py",
    "queuetruth/schemas/models.py",
    "queuetruth/schemas/specs.py",
    "queuetruth/schemas/arrow.py",
]
#: Top-level wheel entries besides the package and its .dist-info (vendored shared libraries from repair tools).
WHEEL_EXTRA_TOP = {"queuetruth.libs"}

#: Never in any artifact: build output, environments, local data, caches, compiled Python, editor and OS litter.
FORBIDDEN_PARTS = {"target", ".venv", ".realdata", ".scratch", "__pycache__", ".git", ".hypothesis", ".pytest_cache"}
FORBIDDEN_GLOBS = [
    "*.pyc",
    "*.pyo",
    ".DS_Store",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "id_rsa*",
    "*.whl",
    "*.tar.gz",
]
#: Market-data and bulk-data formats (SPEC 9: no market data in wheels or the sdist, not even excerpts).
DATA_GLOBS = [
    "*.parquet",
    "*.arrow",
    "*.feather",
    "*.dbn",
    "*.dbn.zst",
    "*.zst",
    "*.gz",
    "*.bz2",
    "*.xz",
    "*.npz",
    "*.npy",
    "*.pcap",
    "*.pcapng",
    "*.itch",
    "*.ITCH50",
    "*.csv",
    "*.jsonl",
]
#: Small hand-written importer and converter fixtures of the Rust test suites (synthetic, SPEC 9) are allowed in
#: the sdist only, each under this size.
SDIST_FIXTURE_GLOB = "crates/*/tests/*"
SDIST_FIXTURE_MAX = 64 * 1024
SDIST_FILE_MAX = 2 * 1024 * 1024
SDIST_MAX = 10 * 1024 * 1024
WHEEL_MAX = 60 * 1024 * 1024
EXTENSION_MAX = 80 * 1024 * 1024

SECRET_PATTERNS = {
    "AWS access key id": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "private key block": re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "PyPI token": re.compile(rb"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{16,}"),
    "Stripe live key": re.compile(rb"\b[rs]k_live_[0-9A-Za-z]{16,}\b"),
    "Anthropic API key": re.compile(rb"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "OpenAI API key": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b"),
    "Slack token": re.compile(rb"\bxox[abprs]-[0-9A-Za-z-]{10,}"),
    "Databento API key": re.compile(rb"\bdb-[A-Za-z0-9]{28,}\b"),
}
TEXT_SUFFIXES = {".py", ".pyi", ".txt", ".md", ".json", ".toml", ".cfg", ".yml", ".yaml", ".rs", ".csv", ".jsonl", ""}

WHEEL_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.]+)-(?P<version>[^-]+)-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$"
)


@dataclass
class Finding:
    """One failed check."""

    artifact: str
    check: str
    message: str


@dataclass
class Report:
    """Every finding plus what was inspected."""

    artifacts: list[dict[str, object]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def fail(self, artifact: str, check: str, message: str) -> None:
        """Records a failed check."""
        self.findings.append(Finding(artifact, check, message))


@dataclass
class Source:
    """What the repository checkout says the artifacts must contain."""

    root: Path
    version: str
    dependencies: list[str]
    python_files: dict[str, bytes]
    schemas: dict[str, bytes]


def load_source(root: Path, report: Report) -> Source:
    """Reads the expected version, dependencies, Python files and JSON Schemas from the checkout."""
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]
    cargo = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))
    cargo_version = cargo.get("workspace", {}).get("package", {}).get("version")
    if cargo_version != version:
        report.fail("source", "version", f"Cargo.toml workspace version {cargo_version!r} != pyproject {version!r}")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (root / "python/queuetruth/_version.py").read_text(encoding="utf-8"))
    if not m or m.group(1) != version:
        report.fail("source", "version", f"python/queuetruth/_version.py says {m and m.group(1)!r}, not {version!r}")
    pkg = root / "python"
    python_files = {
        p.relative_to(pkg).as_posix(): p.read_bytes()
        for p in sorted(pkg.joinpath("queuetruth").rglob("*"))
        if p.is_file() and p.suffix in {".py", ".pyi"} and "__pycache__" not in p.parts
    }
    python_files["queuetruth/py.typed"] = (pkg / "queuetruth/py.typed").read_bytes()
    schemas = {p.name: p.read_bytes() for p in sorted((root / "schemas/json").glob("*.schema.json"))}
    if not schemas:
        report.fail("source", "schemas", "schemas/json holds no *.schema.json")
    return Source(root, version, list(pyproject["project"].get("dependencies", [])), python_files, schemas)


def _dep_name(req: str) -> str:
    return re.split(r"[\s<>=!~;\[(]", req.strip(), maxsplit=1)[0].lower().replace("_", "-")


def check_metadata(art: str, raw: bytes, report: Report, source: Source | None) -> str | None:
    """Core-metadata checks shared by the wheel METADATA and the sdist PKG-INFO; returns the version."""
    text = raw.decode("utf-8")
    head, _, body = text.partition("\n\n")
    msg: Message = HeaderParser().parsestr(head + "\n")
    name, version = msg.get("Name"), msg.get("Version")
    if name != NAME:
        report.fail(art, "metadata.name", f"Name is {name!r}, expected {NAME!r}")
    if source is not None and version != source.version:
        report.fail(art, "metadata.version", f"Version is {version!r}, pyproject.toml says {source.version!r}")
    if msg.get("License-Expression") != LICENSE_EXPRESSION:
        report.fail(art, "metadata.license", f"License-Expression is {msg.get('License-Expression')!r}")
    if any(c.startswith("License ::") for c in msg.get_all("Classifier", [])):
        report.fail(art, "metadata.license", "License :: classifiers must not accompany License-Expression (PEP 639)")
    missing = [f for f in LICENSE_FILES if f not in msg.get_all("License-File", [])]
    if missing:
        report.fail(art, "metadata.license-files", f"License-File entries missing: {missing}")
    if msg.get("Requires-Python") != REQUIRES_PYTHON:
        report.fail(art, "metadata.requires-python", f"Requires-Python is {msg.get('Requires-Python')!r}")
    extras = set(msg.get_all("Provides-Extra", []))
    if extras != EXTRAS:
        report.fail(art, "metadata.extras", f"Provides-Extra is {sorted(extras)}, expected {sorted(EXTRAS)}")
    reqs = msg.get_all("Requires-Dist", [])
    base = [r for r in reqs if "extra ==" not in r]
    names = {_dep_name(r) for r in base}
    if names != BASE_DEPS:
        report.fail(art, "metadata.requires-dist", f"runtime dependencies are {sorted(names)}")
    if source is not None:
        norm = sorted(r.replace(" ", "") for r in base)
        want = sorted(r.replace(" ", "") for r in source.dependencies)
        if norm != want:
            report.fail(art, "metadata.requires-dist", f"Requires-Dist {norm} differ from pyproject {want}")
    summary = msg.get("Summary") or ""
    if not summary or "\n" in summary or len(summary) > 512:
        report.fail(art, "metadata.summary", "Summary must be one non-empty line of at most 512 characters")
    if not (msg.get("Description-Content-Type") or "").startswith("text/markdown"):
        report.fail(art, "metadata.readme", f"Description-Content-Type is {msg.get('Description-Content-Type')!r}")
    if len(body.strip()) < 200:
        report.fail(art, "metadata.readme", "long description (README) is missing or too short")
    urls = msg.get_all("Project-URL", [])
    if not any(u.split(",", 1)[-1].strip().startswith("https://") for u in urls):
        report.fail(art, "metadata.urls", "no Project-URL (add [project.urls] with at least the source repository)")
    return version


def _safe_name(art: str, name: str, report: Report) -> bool:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or "\\" in name:
        report.fail(art, "layout.unsafe-path", f"unsafe member path {name!r}")
        return False
    return True


def _forbidden(art: str, name: str, size: int, report: Report, *, sdist: bool) -> None:
    parts = PurePosixPath(name).parts
    base = parts[-1] if parts else name
    bad = FORBIDDEN_PARTS.intersection(parts[:-1]) | ({base} & FORBIDDEN_PARTS)
    if bad:
        report.fail(art, "content.forbidden", f"{name}: inside {sorted(bad)}")
    if any(fnmatch.fnmatch(base, g) for g in FORBIDDEN_GLOBS):
        report.fail(art, "content.forbidden", f"{name}: forbidden file type")
    if any(fnmatch.fnmatch(base, g) for g in DATA_GLOBS):
        if not (sdist and fnmatch.fnmatch(name, SDIST_FIXTURE_GLOB) and size <= SDIST_FIXTURE_MAX):
            report.fail(art, "content.data", f"{name}: data file ({size} bytes); no market data in artifacts")
    if sdist and size > SDIST_FILE_MAX:
        report.fail(art, "content.size", f"{name}: {size} bytes > {SDIST_FILE_MAX}")


def _secrets(art: str, name: str, data: bytes, report: Report) -> None:
    if PurePosixPath(name).suffix not in TEXT_SUFFIXES:
        return
    for label, pat in SECRET_PATTERNS.items():
        if pat.search(data):
            report.fail(art, "content.secret", f"{name}: looks like a {label}")


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def check_wheel(path: Path, report: Report, source: Source | None) -> dict[str, object]:
    """Layout, tags, RECORD, metadata, licences, package files, extension module and content checks of a wheel."""
    art = path.name
    info: dict[str, object] = {"artifact": art, "kind": "wheel", "bytes": path.stat().st_size}
    m = WHEEL_RE.match(art)
    if not m:
        report.fail(art, "wheel.filename", "file name is not name-version-python-abi-platform.whl")
        return info
    info["platform"] = m["plat"]
    if m["name"] != NAME or m["py"] != PY_TAG or m["abi"] != ABI_TAG:
        report.fail(art, "wheel.tags", f"expected {NAME}-*-{PY_TAG}-{ABI_TAG}-*, got {m['name']}-{m['py']}-{m['abi']}")
    if path.stat().st_size > WHEEL_MAX:
        report.fail(art, "wheel.size", f"{path.stat().st_size} bytes > {WHEEL_MAX}")
    with zipfile.ZipFile(path) as zf:
        members = {i.filename: i for i in zf.infolist() if not i.is_dir()}
        data = {n: zf.read(n) for n in members}
    dist_info = f"{NAME}-{m['version']}.dist-info"
    for n in members:
        if not _safe_name(art, n, report):
            continue
        top = n.split("/", 1)[0]
        if top not in {NAME, dist_info} | WHEEL_EXTRA_TOP:
            report.fail(art, "wheel.layout", f"unexpected top-level entry {top!r}")
        _forbidden(art, n, members[n].file_size, report, sdist=False)
        _secrets(art, n, data[n], report)
    for req in ("METADATA", "WHEEL", "RECORD", "entry_points.txt"):
        if f"{dist_info}/{req}" not in data:
            report.fail(art, "wheel.dist-info", f"{dist_info}/{req} missing")
    if f"{dist_info}/METADATA" in data:
        version = check_metadata(art, data[f"{dist_info}/METADATA"], report, source)
        if version != m["version"]:
            report.fail(art, "wheel.version", f"METADATA Version {version!r} != file name {m['version']!r}")
        info["version"] = version
    if f"{dist_info}/WHEEL" in data:
        wheel = HeaderParser().parsestr(data[f"{dist_info}/WHEEL"].decode())
        if wheel.get("Root-Is-Purelib", "").lower() != "false":
            report.fail(art, "wheel.purelib", "Root-Is-Purelib must be false for a wheel with an extension module")
        tags = set(wheel.get_all("Tag", []))
        if f"{m['py']}-{m['abi']}-{m['plat']}" not in tags and not all(
            f"{m['py']}-{m['abi']}-{p}" in tags for p in m["plat"].split(".")
        ):
            report.fail(art, "wheel.tags", f"WHEEL Tag {sorted(tags)} does not match the file name")
    if f"{dist_info}/RECORD" in data:
        record = {}
        for line in data[f"{dist_info}/RECORD"].decode().splitlines():
            if line.strip():
                rname, rhash, rsize = line.rsplit(",", 2)
                record[rname.strip('"')] = (rhash, rsize)
        for n, blob in data.items():
            if n == f"{dist_info}/RECORD":
                continue
            if n not in record:
                report.fail(art, "wheel.record", f"{n} not listed in RECORD")
            elif record[n] != (_record_hash(blob), str(len(blob))):
                report.fail(art, "wheel.record", f"{n}: hash or size differs from RECORD")
        for n in record.keys() - data.keys():
            report.fail(art, "wheel.record", f"RECORD lists {n}, which is not in the wheel")
    ep = configparser.ConfigParser(delimiters=("=",), interpolation=None)
    ep.optionxform = str  # type: ignore[assignment,method-assign]
    ep.read_string(data.get(f"{dist_info}/entry_points.txt", b"").decode())
    if ep.get("console_scripts", CONSOLE_SCRIPT[0], fallback=None) != CONSOLE_SCRIPT[1]:
        report.fail(art, "wheel.entry-points", f"console script {CONSOLE_SCRIPT[0]} = {CONSOLE_SCRIPT[1]} missing")
    for lic in LICENSE_FILES:
        name = f"{dist_info}/licenses/{lic}"
        if name not in data:
            report.fail(art, "wheel.licenses", f"{name} missing")
        elif source is not None and data[name] != (source.root / lic).read_bytes():
            report.fail(art, "wheel.licenses", f"{name} differs from the checkout")
    for req in WHEEL_REQUIRED:
        if req not in data:
            report.fail(art, "wheel.package", f"{req} missing")
    ext = [n for n in data if re.fullmatch(r"queuetruth/_core(\.abi3\.so|\.pyd)", n)]
    other_native = [
        n for n in data if n.startswith(f"{NAME}/") and n not in ext and re.search(r"\.(so|pyd|dylib|dll)(\.\d+)*$", n)
    ]
    if len(ext) != 1:
        report.fail(art, "wheel.extension", f"expected exactly one queuetruth/_core.abi3.so or _core.pyd, got {ext}")
    if other_native:
        report.fail(art, "wheel.extension", f"unexpected native files in the package: {other_native}")
    if ext:
        so = data[ext[0]]
        info["extension_bytes"] = len(so)
        if len(so) > EXTENSION_MAX:
            report.fail(art, "wheel.extension", f"{ext[0]} is {len(so)} bytes > {EXTENSION_MAX}")
        if b"https://json-schema.org/draft/2020-12/schema" not in so:
            report.fail(art, "wheel.schemas", "the extension module embeds no JSON Schema (schemas/json missing?)")
        if source is not None:
            missing = [n for n, blob in source.schemas.items() if blob not in so]
            if missing:
                report.fail(art, "wheel.schemas", f"{len(missing)} JSON Schemas not embedded, e.g. {missing[:3]}")
    if source is not None:
        pkg = {n: b for n, b in data.items() if n.startswith(f"{NAME}/") and n.endswith((".py", ".pyi", "py.typed"))}
        for n, blob in source.python_files.items():
            if n not in pkg:
                report.fail(art, "wheel.package", f"{n} (in the checkout) missing")
            elif pkg[n] != blob:
                report.fail(art, "wheel.package", f"{n} differs from the checkout")
        for n in pkg.keys() - source.python_files.keys():
            report.fail(art, "wheel.package", f"{n} is not in the checkout (stale build input?)")
    info["files"] = len(data)
    return info


def check_sdist(path: Path, report: Report, source: Source | None) -> dict[str, object]:
    """Layout, metadata, build inputs and content checks of the sdist."""
    art = path.name
    info: dict[str, object] = {"artifact": art, "kind": "sdist", "bytes": path.stat().st_size}
    if path.stat().st_size > SDIST_MAX:
        report.fail(art, "sdist.size", f"{path.stat().st_size} bytes > {SDIST_MAX}")
    m = re.fullmatch(rf"{NAME}-(?P<version>[^-]+)\.tar\.gz", art)
    if not m:
        report.fail(art, "sdist.filename", f"expected {NAME}-VERSION.tar.gz")
        return info
    prefix = f"{NAME}-{m['version']}/"
    data: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as tf:
        for ti in tf.getmembers():
            if ti.issym() or ti.islnk():
                report.fail(art, "sdist.layout", f"{ti.name}: links are not allowed")
                continue
            if not ti.isfile():
                continue
            if not _safe_name(art, ti.name, report):
                continue
            if not ti.name.startswith(prefix):
                report.fail(art, "sdist.layout", f"{ti.name}: outside {prefix}")
                continue
            f = tf.extractfile(ti)
            data[ti.name[len(prefix) :]] = f.read() if f else b""
    for n, blob in data.items():
        _forbidden(art, n, len(blob), report, sdist=True)
        _secrets(art, n, blob, report)
        if re.search(r"\.(so|pyd|dylib|dll)(\.\d+)*$", n):
            report.fail(art, "sdist.native", f"{n}: compiled binary in the sdist")
    for req in ("PKG-INFO", "pyproject.toml", "Cargo.toml", "Cargo.lock", "README.md", *LICENSE_FILES):
        if req not in data:
            report.fail(art, "sdist.required", f"{req} missing")
    if "PKG-INFO" in data:
        version = check_metadata(art, data["PKG-INFO"], report, source)
        if version != m["version"]:
            report.fail(art, "sdist.version", f"PKG-INFO Version {version!r} != file name {m['version']!r}")
        info["version"] = version
    if "Cargo.toml" in data:
        ws = tomllib.loads(data["Cargo.toml"].decode()).get("workspace", {})
        for member in ws.get("members", []):
            if f"{member}/Cargo.toml" not in data:
                report.fail(art, "sdist.required", f"workspace member {member}/Cargo.toml missing")
    if "crates/cli/build.rs" in data:
        embedded = sorted(n for n in data if fnmatch.fnmatch(n, "schemas/json/*.schema.json"))
        if not embedded:
            report.fail(
                art,
                "sdist.build-inputs",
                "schemas/json/*.schema.json missing: crates/cli/build.rs embeds them with include_str!, so a wheel "
                "built from this sdist has no JSON Schemas (`schema show` fails, `doctor` reports problems)",
            )
        elif source is not None:
            names = {PurePosixPath(n).name for n in embedded}
            if names != set(source.schemas):
                report.fail(art, "sdist.build-inputs", "schemas/json differs from the checkout")
    if source is not None:
        for n, blob in source.python_files.items():
            if f"python/{n}" not in data:
                report.fail(art, "sdist.package", f"python/{n} missing")
            elif data[f"python/{n}"] != blob:
                report.fail(art, "sdist.package", f"python/{n} differs from the checkout")
        for lic in LICENSE_FILES:
            if lic in data and data[lic] != (source.root / lic).read_bytes():
                report.fail(art, "sdist.licenses", f"{lic} differs from the checkout")
    info["files"] = len(data)
    return info


def collect(paths: list[Path]) -> list[Path]:
    """Expands directories into the wheels and sdists they hold."""
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(sorted(p.glob("*.whl")) + sorted(p.glob("*.tar.gz")))
        elif p.is_file():
            out.append(p)
        else:
            raise FileNotFoundError(p)
    return out


def main(argv: list[str] | None = None) -> int:
    """Checks every artifact and prints the findings."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dist", nargs="+", type=Path, help="wheel, sdist or directory of them")
    ap.add_argument("--source", type=Path, help="repository checkout to compare against")
    ap.add_argument("--require-sdist", action="store_true", help="fail unless exactly one sdist is given")
    ap.add_argument(
        "--require-platform",
        action="append",
        default=[],
        metavar="GLOB",
        help="fail unless some wheel's platform tag matches GLOB (repeatable), e.g. 'macosx_*_arm64'",
    )
    ap.add_argument("--json", action="store_true", help="print a JSON report instead of text")
    args = ap.parse_args(argv)
    try:
        artifacts = collect(args.dist)
    except FileNotFoundError as e:
        print(f"check_dist: no such file or directory: {e}", file=sys.stderr)
        return 2
    if not artifacts:
        print("check_dist: no wheel or sdist found", file=sys.stderr)
        return 2
    report = Report()
    try:
        source = load_source(args.source.resolve(), report) if args.source else None
        for a in artifacts:
            if a.name.endswith(".whl"):
                report.artifacts.append(check_wheel(a, report, source))
            elif a.name.endswith(".tar.gz"):
                report.artifacts.append(check_sdist(a, report, source))
            else:
                report.fail(a.name, "usage", "neither a wheel nor an sdist")
    except (OSError, zipfile.BadZipFile, tarfile.TarError, tomllib.TOMLDecodeError, KeyError) as e:
        print(f"check_dist: cannot read artifacts: {e!r}", file=sys.stderr)
        return 2
    versions = {a.get("version") for a in report.artifacts if a.get("version")}
    if len(versions) > 1:
        report.fail("all", "version", f"artifacts disagree on the version: {sorted(versions)}")
    sdists = [a for a in report.artifacts if a["kind"] == "sdist"]
    if args.require_sdist and len(sdists) != 1:
        report.fail("all", "sdist.count", f"expected exactly one sdist, got {len(sdists)}")
    plats = [str(a.get("platform", "")) for a in report.artifacts if a["kind"] == "wheel"]
    for g in args.require_platform:
        if not any(fnmatch.fnmatch(p, g) for p in plats):
            report.fail("all", "wheel.platforms", f"no wheel for platform {g!r} (have {plats})")
    if args.json:
        doc = {"ok": not report.findings, "artifacts": report.artifacts}
        doc["findings"] = [asdict(f) for f in report.findings]
        print(json.dumps(doc, indent=2))
    else:
        for a in report.artifacts:
            extra = f", extension {a['extension_bytes']:,} bytes" if "extension_bytes" in a else ""
            print(f"{a['artifact']}: {a['kind']}, {a['bytes']:,} bytes, {a.get('files', '?')} files{extra}")
        for f in report.findings:
            print(f"FAIL {f.artifact} [{f.check}] {f.message}")
        print("check_dist: ok" if not report.findings else f"check_dist: {len(report.findings)} failed check(s)")
    return 0 if not report.findings else 1


if __name__ == "__main__":
    sys.exit(main())
