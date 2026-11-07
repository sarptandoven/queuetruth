"""`scripts/nightly/release_assets.sh` finds a draft release (atlas.yml keeps the Nasdaq days in one) by its pending tag
name, since `releases/tags/TAG` does not return drafts. `gh` is a fake on PATH that answers like the REST API."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "release_assets.sh"

# the fake prints what the real `gh api ... --jq` would print for this repository: a published release `atlas-days`
# (id 1, one asset) and a draft `atlas-days-nasdaq` (id 2, one asset)
FAKE_GH = r"""#!/usr/bin/env bash
args="$*"
case "$args" in
  *"releases/tags/atlas-days "*|*"releases/tags/atlas-days") echo 1 ;;
  *"releases/tags/"*) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
  *"releases?per_page=100"*)
    # the select() of the script keeps drafts with the asked pending tag name
    if [[ "$args" == *'"atlas-days-nasdaq"'* ]]; then echo 2; fi ;;
  *"releases/1/assets"*) echo "11 day-2026-10-09.tar.gz" ;;
  *"releases/2/assets"*) echo "21 nasdaq-day-2020-01-30.tar.gz" ;;
  *"releases/assets/21"*) printf 'draft-bytes' ;;
  *) echo "unexpected: $args" >&2; exit 2 ;;
esac
"""


def _run(tmp: Path, *args: str) -> subprocess.CompletedProcess:
    gh = tmp / "bin" / "gh"
    gh.parent.mkdir(exist_ok=True)
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{gh.parent}:{os.environ['PATH']}", "GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "x"}
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False)


def test_published_and_draft_releases_and_a_missing_one(tmp_path: Path) -> None:
    assert _run(tmp_path, "names", "atlas-days").stdout == "day-2026-10-09.tar.gz\n"
    assert _run(tmp_path, "names", "atlas-days-nasdaq").stdout == "nasdaq-day-2020-01-30.tar.gz\n"
    assert _run(tmp_path, "names", "atlas-days-other").stdout == ""
    assert _run(tmp_path, "exists", "atlas-days").returncode == 0
    assert _run(tmp_path, "exists", "atlas-days-nasdaq").returncode == 0
    assert _run(tmp_path, "exists", "atlas-days-other").returncode == 1
    r = _run(tmp_path, "download", "atlas-days-nasdaq", str(tmp_path / "dl"), "nasdaq-day-2020-01-30.tar.gz")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "dl" / "nasdaq-day-2020-01-30.tar.gz").read_text() == "draft-bytes"
