"""Virtual environments of the nightly checks (one per engine version, built with uv) and the PyPI lookups they need.

Every environment lives in `<state>/venvs/<name>` with a marker `.qt-nightly-env.json` holding the fingerprint of
what was installed (interpreter, requirements, wheel digests). A run reuses an environment whose fingerprint matches
and rebuilds it otherwise (or always with `--refresh-envs`), so a nightly costs one venv build per change, not per
night. Installs pass `--no-config` (the repository's uv settings never leak into the engine venvs) and an
`--exclude-newer` cutoff 14 days back (the repository's supply-chain rule), lifted only for the release candidate
under test.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import platform
import re
import shutil
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MARKER = ".qt-nightly-env.json"
PYPI_JSON = "https://pypi.org/pypi/{name}/json"
_VER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?(?:\.post(\d+))?$")
_PRE_RANK = {"a": 0, "b": 1, "rc": 2, None: 3}
_MAC_TAG = re.compile(r"macosx_(\d+)_(\d+)_(arm64|x86_64|universal2)")


class EnvError(RuntimeError):
    """An environment could not be built or a lookup failed (the check reports status `error`)."""


def version_key(v: str) -> tuple[int, ...] | None:
    """Sort key of a release version `X.Y.Z[aN|bN|rcN][.postN]`; None for dev releases and anything else."""
    m = _VER.match(v)
    if not m:
        return None
    major, minor, patch, pre, pre_n, post = m.groups()
    return (int(major), int(minor), int(patch), _PRE_RANK[pre], int(pre_n or 0), int(post or 0))


def newest_installable(
    releases: dict[str, list[dict[str, Any]]], major: int, installable: Callable[[list[dict[str, Any]]], bool]
) -> str | None:
    """Newest version of `major` (pre-releases included) whose files pass `installable` (e.g. a wheel exists for this
    interpreter and platform): a release candidate published without a wheel for this host is skipped, not errored on
    every night."""
    ordered = sorted(((k, v) for v in releases if (k := version_key(v)) is not None and k[0] == major), reverse=True)
    for _, v in ordered:
        files = [f for f in releases[v] if not f.get("yanked", False)]
        if files and installable(files):
            return v
    return None


def wheel_for_host(
    files: list[dict[str, Any]], py_tag: str, system: str, machine: str, os_major: int | None
) -> dict[str, Any] | None:
    """The wheel of `files` this host can install: `pick_wheel` on macOS; elsewhere a wheel of `py_tag` whose
    platform tag names this machine (`x86_64`, `aarch64`)."""
    if system == "Darwin":
        return pick_wheel(files, py_tag, machine, os_major)
    arch = "aarch64" if machine in ("arm64", "aarch64") else machine
    for f in files:
        fn = f.get("filename", "")
        if not fn.endswith(".whl") or f.get("yanked"):
            continue
        parts = fn[:-4].split("-")
        if len(parts) >= 5 and parts[-3] == py_tag and arch in parts[-1] and "linux" in parts[-1]:
            return {**f, "needs_retag": False}
    return None


def fetch_json(url: str, timeout: float = 30.0) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "queuetruth-nightly"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (OSError, ValueError) as e:
        raise EnvError(f"GET {url}: {e}") from e


def pypi_project(name: str, fetch: Callable[[str], dict[str, Any]] = fetch_json) -> dict[str, Any]:
    return fetch(PYPI_JSON.format(name=name))


def pypi_release(name: str, version: str, fetch: Callable[[str], dict[str, Any]] = fetch_json) -> dict[str, Any]:
    return fetch(f"https://pypi.org/pypi/{name}/{version}/json")


def mac_major() -> int | None:
    """macOS major version of this host, or None off macOS."""
    rel = platform.mac_ver()[0]
    return int(rel.split(".")[0]) if rel else None


def pick_wheel(files: list[dict[str, Any]], py_tag: str, machine: str, os_major: int | None) -> dict[str, Any] | None:
    """The macOS wheel of `py_tag` (e.g. `cp313`) for `machine` from a PyPI release's `urls`, or None.

    Returns the file entry with an extra `needs_retag` flag: the wheel's minimum macOS (its platform tag) is newer
    than this host although the extension modules may be built for older systems (nautilus_trader 1.231.0's arm64
    wheel is tagged macosx_26_0 with every module built for 11.0); such a wheel is installed under a macosx_11_0
    file name, which is what scripts/nt_crosscheck/requirements.txt documents.
    """
    arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
    for f in files:
        fn = f.get("filename", "")
        if not fn.endswith(".whl") or f.get("yanked"):
            continue
        parts = fn[:-4].split("-")
        if len(parts) < 5 or parts[-3] != py_tag:
            continue
        m = _MAC_TAG.search(parts[-1])
        if not m or m.group(3) not in (arch, "universal2"):
            continue
        need = os_major is not None and int(m.group(1)) > os_major
        return {**f, "needs_retag": need}
    return None


def retagged_name(filename: str) -> str:
    """`...-macosx_26_0_arm64.whl` -> `...-macosx_11_0_arm64.whl`."""
    return _MAC_TAG.sub(lambda m: f"macosx_11_0_{m.group(3)}", filename)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_or_download_wheel(
    entry: dict[str, Any], dest_dir: Path, search_dirs: list[Path], offline: bool, log: Callable[[str], None]
) -> Path:
    """Local path of the wheel `entry` (a PyPI file entry from `pick_wheel`), under its retagged name when needed.

    Looks in `search_dirs` and `dest_dir` for a file of the (retagged) name whose sha256 equals PyPI's digest, and
    downloads it into `dest_dir` otherwise (refused when `offline`). A file with the right name and the wrong digest
    is ignored (and replaced in `dest_dir`).
    """
    name = retagged_name(entry["filename"]) if entry.get("needs_retag") else entry["filename"]
    want = entry["digests"]["sha256"]
    for d in [*search_dirs, dest_dir]:
        p = d / name
        if p.is_file():
            got = sha256_file(p)
            if got == want:
                log(f"wheel {p} (sha256 ok)")
                return p
            log(f"ignoring {p}: sha256 {got} != PyPI {want}")
    if offline:
        raise EnvError(f"{name} is not available locally and --offline forbids downloading it")
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / (name + ".part")
    log(f"downloading {entry['url']} ({entry.get('size', '?')} bytes)")
    try:
        with urllib.request.urlopen(entry["url"], timeout=600) as r, tmp.open("wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise EnvError(f"download {entry['url']}: {e}") from e
    got = sha256_file(tmp)
    if got != want:
        tmp.unlink(missing_ok=True)
        raise EnvError(f"downloaded {name}: sha256 {got} != PyPI {want}")
    out = dest_dir / name
    tmp.replace(out)
    return out


def exclude_newer(today: dt.date, days: int = 14) -> str:
    """RFC 3339 cutoff `days` before `today` (00:00 UTC), the repository's two-week supply-chain rule."""
    return (today - dt.timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")


@dataclass
class EnvSpec:
    """What one venv holds. `installs` are argument lists appended to `uv pip install --no-config --python <py>`."""

    name: str
    python: str
    installs: list[list[str]]
    fingerprint: dict[str, Any] = field(default_factory=dict)
    #: appended to every install but not part of the digest (the daily `--exclude-newer` cutoff: an environment is
    #: rebuilt when what it should hold changes, not every day)
    unhashed: list[str] = field(default_factory=list)

    def digest(self) -> str:
        doc = {"python": self.python, "installs": self.installs, "fingerprint": self.fingerprint}
        return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def venv_python(venv: Path) -> Path:
    return venv / "bin" / "python"


def env_is_current(venv: Path, spec: EnvSpec) -> bool:
    m = venv / MARKER
    if not m.is_file() or not venv_python(venv).exists():
        return False
    try:
        return json.loads(m.read_text()).get("digest") == spec.digest()
    except (OSError, ValueError):
        return False


def ensure_env(
    venv: Path,
    spec: EnvSpec,
    uv: str,
    run: Callable[[list[str]], int],
    refresh: bool,
    log: Callable[[str], None],
) -> tuple[Path, bool]:
    """Python of the venv for `spec`, built when missing, stale or `refresh`; returns (python, rebuilt).

    `run(argv) -> exit code` runs a command (the runner logs it to the check's log). The marker is written last,
    so a failed build is rebuilt from scratch next time.
    """
    if not refresh and env_is_current(venv, spec):
        log(f"venv {venv} is current")
        return venv_python(venv), False
    if venv.exists():
        shutil.rmtree(venv)
    if run([uv, "venv", "--no-config", "--quiet", "--python", spec.python, str(venv)]) != 0:
        raise EnvError(f"uv venv {venv} failed")
    py = venv_python(venv)
    for args in spec.installs:
        if run([uv, "pip", "install", "--no-config", "--python", str(py), *args, *spec.unhashed]) != 0:
            raise EnvError(f"uv pip install {' '.join(args)} failed")
    (venv / MARKER).write_text(
        json.dumps({"digest": spec.digest(), "spec": spec.__dict__}, indent=1, sort_keys=True) + "\n"
    )
    return py, True


def python_tag(python: str, run_capture: Callable[[list[str]], str]) -> str:
    """`cp313` for the interpreter `python`."""
    out = run_capture([python, "-c", "import sys; print(f'cp{sys.version_info[0]}{sys.version_info[1]}')"]).strip()
    if not re.fullmatch(r"cp\d+", out):
        raise EnvError(f"cannot read the version of {python}: {out!r}")
    return out
