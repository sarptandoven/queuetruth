#!/usr/bin/env bash
# Creates the hftbacktest venvs the integration runs in (never the queuetruth venv):
#   .scratch/hbt-venv      released hftbacktest 2.4.4 + numba + numpy, pinned (the `make hbt-crosscheck` venv)
#   .scratch/hbt-venv-312  the #312-patched build, from a wheel built by upstream/verify.sh (optional)
#
# Usage: integrations/hftbacktest/setup_venvs.sh [path/to/patched.whl]
# Env: PYTHON_BIN (default python3.13).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
PY=${PYTHON_BIN:-python3.13}
cd "$REPO"
# the versions the documented evidence ran on (UPSTREAM.md, README.md)
PINS=(numba==0.68.0 llvmlite==0.50.0 numpy==2.5.3)
mk() { # dir
  [ -x "$1/bin/python" ] || uv venv -q "$1" --python "$PY"
  uv pip install -q --python "$1" "${PINS[@]}"
}
mk .scratch/hbt-venv
# --no-deps: hftbacktest's polars/holoviews/matplotlib pins are not needed by the drivers
uv pip install -q --python .scratch/hbt-venv --no-deps hftbacktest==2.4.4
echo "released: $(.scratch/hbt-venv/bin/python -c 'import importlib.metadata as m; print(m.version("hftbacktest"))')"
if [ $# -ge 1 ]; then
  [ -f "$1" ] || { echo "QT-USAGE-001: wheel not found: $1" >&2; exit 2; }
  mk .scratch/hbt-venv-312
  uv pip install -q --python .scratch/hbt-venv-312 --no-deps --reinstall "$1"
  echo "patched: $1"
  .scratch/hbt-venv-312/bin/python -I "$HERE/upstream/repro_312.py" || {
    echo "the wheel still reproduces #312" >&2
    exit 1
  }
fi
