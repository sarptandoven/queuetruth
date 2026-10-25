#!/bin/bash
# Wrapper for launchd / cron: a predictable PATH (uv, cargo, Homebrew), the checkout as working directory, the Mac kept
# awake for the run (caffeinate -i), and the exit code of nightly.py passed through. Prints the run's own log lines;
# every check's output goes to <state>/runs/<nightly_id>/checks/<name>/log.txt.
#
#   bash scripts/nightly/nightly.sh run --mode full [--state DIR] [...]   (all arguments go to nightly.py)
#
# QT_NIGHTLY_PYTHON picks the interpreter that runs nightly.py and creates every venv (default: Homebrew python3.13,
# else the first python3.13, 3.12, 3.11 or python3 on PATH).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PY="${QT_NIGHTLY_PYTHON:-}"
if [ -z "$PY" ]; then  # nightly.py needs CPython >= 3.11 (macOS /usr/bin/python3 is 3.9)
  for c in /opt/homebrew/bin/python3.13 python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
  done
fi
cd "$REPO"
if command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -i "$PY" -I "$HERE/nightly.py" "$@"
fi
exec "$PY" -I "$HERE/nightly.py" "$@"
